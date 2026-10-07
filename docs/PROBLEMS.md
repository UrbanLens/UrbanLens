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

## P9 — Land-use-area boundary geometry is not drawn, pending a map-overlay decision

`id: P9` · `status: open` · `updated: 2026-10-06`

Previously titled "REData's `?limit=` param is inert client-side, and land-use-area boundary geometry
needs a map-overlay decision". The `limit` half is closed: REData applies `limit` per provider (its
`parse_result_limit`, clamped to 200, from `a682b74f`, first tagged v0.3.4), and production REData has run v0.3.4
since 2026-10-06 15:21Z, so it no longer ignores `limit`. `RedataLocationContextGateway.near_point` caps each
provider's rows the same way (`cap_per_provider`, `redata_context_gateway.py:98`), so a panel's "N+" floor is
decided per provider (`redata_panel.at_limit`); that client-side cap is on `release/v_0_9_0` only and ships
with 0.9.0, as `origin/main` (0.8.0) has no `cap_per_provider`.

**Land-use-areas' boundary geometry is still not rendered, on purpose.** The category chips already
shown on the Property Records card come from a different, already-consumed field; rendering the
actual polygon needs a map-overlay UX decision (a new layer? a toggle? which existing
boundary-rendering chain, if any) that wasn't the sweep's to make. Flagging for product input rather
than guessing at it.

## P11 — Frontend TS audit: its correctness bullets are fixed, the structural debt it found is not

`id: P11` · `status: open` · `updated: 2026-10-04`

Previously titled "Frontend TS audit: a few correctness bullets and structural debt found but not
fixed", before that "84 raw `fetch()` calls bypass `fetch-json.ts`, and 'all the wrappers are gone'
was a count, not a search", and before that "~40 raw `fetch()` calls bypass `fetch-json.ts` and fail
silently; Organize's Media tab is unwired dead UI".

Full-tree audit of `dashboard/frontend/ts/` (every file read, eight passes, starting 2026-08-15).
Every correctness bullet it found is fixed; most of that history moved to
`archive/PROBLEMS-ARCHIVE.md` (2026-09-15). The last three were fixed 2026-10-02: the article
WYSIWYG canvas rewrote untouched source on its first update (`shared/article-source.ts` now writes
back only changed blocks), photo-scan preview uploads had no progress, and a Media tile dropped on
the pin map had no pending state (`shared/media-map-drop.ts`). What's left is structural:

**Structural (no user-visible symptom):**

- **Raw `fetch()` calls: audited 2026-10-02, the silent failures fixed, the migration half done.**
  `grep -rnE '(^|[^.A-Za-z0-9_])fetch\(' --include=*.ts src/urbanlens/dashboard/frontend/ts`, minus
  `*.test.ts`, `shared/fetch-json.ts` and comment lines, found 144 calls in 47 files at `07c4210a5`
  (not the 145/48 first counted); 119 remain. "Silent" is narrower than it looks: `wrapFetch` in
  `shared/site-runtime.ts` toasts every unmarked raw call's non-2xx or network failure on every
  `base.html` page, so the real silent cases are a control left stuck, success shown after a
  failure, and the `auth_base.html` pages (login, signup, 2FA, set-password, reset), which load no
  `core.js` and so have no net. Each call, by its line at `07c4210a5`:
  - **55 need the raw `Response`** (a status that means something - 409, 410, 413, 429, 204, 202 -
    `redirected`, a stream, bytes, a body read whatever the status, per-item outcomes, WebAuthn and
    E2EE protocol). All handle failure. `e2ee-client.ts` is 17 of them.
  - **4 failed silently or left the UI stuck, now fixed:** Settings' "Add passkey unlock" (account
    without a password) never re-enabled its button on a network failure, because
    `showPasskeyEnrollDialog` never settled (`e2ee-client.ts:826`); `showUnlockDialog` never
    appeared when the key bundle could not be fetched, leaving the set-password page's passkey
    button disabled with no message (`:668`); a failed bulk-delete undo left the toast's Undo button
    disabled (`map-page.ts:3527`); a failed location switch/merge re-enabled its button still
    reading "Switching..."/"Merging..." (`:6408`). Tests in `e2ee-client.test.ts` and
    `map-page.contract.test.ts` (the entry is not importable, so it is checked as text).
  - **33 are background or best-effort reads** where silence is the intent: typeahead, polls,
    enrichment, telemetry, optional layers.
  - **52 could move to `fetch-json.ts`; 24 have**, plus the undo above. The 28 left (7 in
    `map-annotations.ts`, 4 in `map-page.ts`, 4 in `markup-toolbar.ts`, 4 in `pin-list-detail.ts`,
    one each in nine more files) each read their body in a way `fetchJson` would change - a
    tolerant `.json().catch()`, a `resp.ok === false` on a 200, the raw error text as the message,
    a `null` the types would have to handle - so each needs its own decision, and most sit in entry
    modules no behavioural test can import.

  The net itself was wrong in two ways, also fixed: it reported a request its own caller aborted
  as "Request timed out.", so every superseded request toasted (an `@mention` lookup per
  keystroke, the infrastructure layer per pan, a MapLibre own-tile dropped on pan, a closed import
  dialog's poll); it now stays quiet unless the abort reason is a `TimeoutError`, which
  `fetchJson`'s timeout now uses. And three 409s that ask a question - delete this pin's children
  too, add this many pins to a list, move the pin out of its wiki place - raised "Request failed
  (HTTP 409)." beside the dialog; those requests now report their own failures. Organize's
  priority save toasted a refusal's raw JSON; it now toasts the sentence.

  **The audit's other open items, fixed 2026-10-02 (merge `d7c79a092` and the commits before it):**
  - An expired session reading as success: P192, archived.
  - Background calls no longer toast through the net: the pins-meta poll (it switches on the offline
    indicator instead, and says "session ended" once on a 401), the geolocation-visit and map-position
    saves, the Wikipedia and place-details enrichment, the safety-map view save, own tiles.
    `shared/fetch-net-callers.contract.test.ts` fails if one of them loses its opt-out.
  - One toast per failure: 83 calls (60 raw, 23 through `fetch-json`) toasted their own message and got
    the net's too; all are marked, raw ones through the new `fetchResponse()`.
  - The smaller ones: inline-image uploads show "Uploading image..." at the cursor; a failed import
    confirm returns the wizard to its preview; a failed pin-list reorder reverts to the last saved order;
    markup says "saved, but the map could not show it" when only the reload failed; the unlock dialog says
    the keys could not be loaded rather than blaming the password; organize's priority save says it
    couldn't reach the server instead of "Failed to fetch".
  - Found by browser-verifying the P11 merge, and fixed: the map's bulk-delete Undo rendered as literal
    `<button>` text since toastr escaping came on (now a real node, `toastWithAction`); "Delete child pins
    too?" was sometimes cancelled at once by the previous dialog's late `close` event (1 in ~23); renaming
    through the Edit Pin dialog left the page heading stale; the chip and parent/child label pickers had no
    keyboard navigation (`shared/suggestion-keys.ts`).

  The last of them, fixed 2026-10-04: the three review queues (location suggestions, merge suggestions, import
  failures) appended `<a ... class="toast-undo-btn">View pin</a>` to a toast's message, which toastr showed as
  text. Their three copies of `_toast` are now `services/core/htmx_toasts.queue_toast`, which sends the link as
  `showToast.link`. Each queue's test asserts the link and a message with no markup. Checked in a browser on dev:
  accepting a location suggestion shows a "View pin" button that opens the new pin.

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

The inline template `<script>` this audit's scope excluded was P34's; none is left (archived
2026-10-02).

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

## P14 — Historical `pin_images/` files whose Image row is gone: `sweep_unnamed_pin_images` exists, not yet run on any environment

`id: P14` · `status: open, handed to infrastructure` · `updated: 2026-10-04`

**Jess, 2026-10-02: report and delete in one go.** The runs are the infrastructure repo's (N33,
`docs/handoffs/infrastructure-jess-decisions-2026-10-02.md`): `sweep_unnamed_pin_images`, then `--delete`, on staging
then production. Close this when their counts and sizes come back.

On `development_main` (2026-10-04) the report found 90 unnamed files under `pin_images/`, under 0.1 MiB in all. The
`--delete` run there was not made: it was refused as an irreversible deletion pending Jess's go-ahead.

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

**The sweep exists (2026-09-29), and has not been run anywhere.** `manage.py sweep_unnamed_pin_images` walks
`pin_images/` against every Image row's four columns and reports the unnamed files and their size; `--delete`
removes them. It keeps a file younger than the Celery hard limit and one an undo inside its retention window
mentions, the same rules as the hourly icon sweep (`stored_field._may_delete`). It is not on the beat schedule,
because its cost follows the table. What is left is running it once per environment, reporting first:
`docker exec <app> python manage.py sweep_unnamed_pin_images`, then `--delete`. Tests:
`test_pin_image_orphan_sweep.py`.

---

## P19 — Audit residue: group chats lack direct messages' features, and the hypothesis strategies are barely shared

`id: P19` · `status: open` · `updated: 2026-10-02`

Previously titled "Full-codebase audit: re-verification pass (2026-07-25)", then "Audit re-verification's
residual gaps: a 1,100-line `_dark.scss`, a stub AI gateway, and a few maintainability gaps".

What remains of the findings in `docs/audits/codebase-audit.md`, each re-checked against the code on
2026-10-02:

- **Unit 21/22/23**: `models/pin/viewset.py`'s post-`get_object()` ownership re-check is kept
  deliberately (2026-09-06) - unreachable while `get_queryset` scopes to `profile__user`, a backstop for
  the day that filter widens; both sites say so. `GroupMessage` still carries no
  images/markup_map/location_mentions/reply_to fields. **Jess, 2026-10-02: later**, so it stays here.
- **Unit 25**: no moderation UI for AI-flagged trivia questions - decided against, not just unbuilt (see
  `docs/designs/drafts/trivia.md`'s "Known gaps").
- **Unit 34**: 12 of the 175 test files that use `@given` import the shared `strategies.py`
  (`grep -rl "@given" src/urbanlens --include='test_*.py'`, then grep those for
  `tests.hypothesis.strategies`). The earlier "~30/111" did not reproduce.

Ruled on by Jess on 2026-10-02 and moved out:

- "Trip Updated" and "Community Wiki Updated" control nothing: build both (P197).
- A departed player vanishes from the scoreboard: list them as "Left" and keep the game in their history (P198).
- Admins see only their own subscription grants: every site admin sees and revokes every grant (P199).
- Postgres restore tooling: the infrastructure repo owns it
  (`docs/handoffs/infrastructure-jess-decisions-2026-10-02.md`).
- `services/ai/huggingface.py`: removed, with its two settings and its row on the setup page. Production's secrets still
  carry `UL_HUGGINGFACE_AI_*`, which the settings model now ignores (`extra="ignore"`).
- `_dark.scss`'s 1,095 lines of overrides: leave it. Measured on 15 signed-in pages: of 413 rules, 86 changed
  something, 26 matched but changed nothing, 217 matched nothing, and 83 depend on a state the probe cannot reach.

Removed on 2026-10-02, measured rather than assumed:

- The "Pin Shared" row's WhatsApp/SMS toggles never reached map shares, whose in-app and email delivery that row
  governs: the text-alert check looked up `map_shared`, which has no toggles. `PREFERENCE_TYPE_FOR` in
  `notification_text_alerts.py` routes it to `pin_shared` (two tests in `test_notification_text_alerts.py`).

- Bulk accept/reject already surfaced per-item failures: both endpoints run each row through
  `services.core.bulk_outcome.run_each` and the page toasts failed rows apart from skipped ones
  (`reportBulkOutcome`). The bullet was stale.
- `TripActivity.order`: adds and reorders already locked the trip, but appends took the activity
  *count*, which deletions make stale (after two deletions a new activity sorted into the middle), and
  `copy_list_pins_to_trip` took the count with no lock and no `max_trip_activities` check. All appends
  now go through `trip_activities.reserve_activity_positions` (lock, cap the whole batch, `Max(order)+1`).
  No `(trip, order)` unique constraint: reorders renumber only non-completed activities, so completed
  ones legitimately share positions with them, and 83 test call sites create activities with the
  default `order=0`.
- Trip invites and calendar push are linear, not N+1. `trip_crud.invite_members`: 42 queries for 3
  invitees, 158 for 12 (~13 each: a locked seat reservation, the membership's achievement read, and a
  notification whose preference, identity and mute checks are per recipient); the calendar import's
  `_invite_participants`: 47 and 181, in a Celery task. Both are bounded by `max_trip_members`
  (default 10, at most 100). `push_auto_synced_trip_changes`: 12 queries and 4 Calendar calls for 3
  scheduled activities, 21 and 13 for 12 - one link write and one API call per activity, which one event
  per activity needs.
- SpotGuessr leave/kick/lobby cancel is built (`docs/FEATURES.md`). Building it found that Trivia's
  round completion counted a departed player's answer, so a player who answered and left could reveal
  the round before someone still playing had answered; fixed in both games.
- `test_trivia_wiki_incorporation.py` has property tests for the threshold. They found that the sweep
  starved: it took the lowest-pk unprocessed questions without filtering on score, and never marked the
  ones below the threshold, so a batch's worth of them blocked every later question. The sweep now
  filters on `voting.score_expression()` in SQL.

## P24 — A campus pin's CRIS detail fetches stop at a per-pass cap, and a child the site's roster misses fetches its own

`id: P24` · `status: open` · `updated: 2026-10-04`

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

**Narrowed 2026-09-24: the site fetch answers the campus's building children.** Before, each
building child fetched its own `cris_building_usn` row (a 200 m lookup, its building's detail and
the site record's detail), and on dev HRSH's children made 4,548 REData calls in three hours,
1,595 rate-limited and 2,972 failed, leaving BLDG 166, 152 and 67 with no row at all. The
site-scope payload now keeps a `campus_buildings` roster (every building the pass resolved, with
its attributes, CRIS point and whether its detail is in the payload), and:

- as the site fetch lands, and after a sweep creates building pins
  (`external_data.seed_site_descendants`), every nested location whose building footprint holds a
  roster building's CRIS point, or that stands within `BUILDING_MATCH_METERS` of one, gets its card
  written, keyed as its own fetch keys it and dated as the site's row. A child's fresh row whose
  media half is filled is left alone;
- a child's own panel fetch (`CrisBuildingPanelSource.adopt_site_answer`) fills its media half from
  the same payload: no REData call for a building the pass detailed, one detail fetch for one it did
  not. Its documents are queued for extraction then, not when the site fetch runs. With no site
  answer, the child fetches the site once for every sibling. A site-scope fetch runs under
  `coalesced`, so a child waits on a site fetch already in flight rather than starting another;
- background enrichment takes a nested location's card from its site before looking it up.

Measured in `src/urbanlens/dashboard/tests/hypothesis/test_cris_campus_seeding.py`, REData faked at its HTTP boundary
(six buildings): the site fetch costs 1 lookup, 7 details and 1 bulk queue. Before, each child's
fetch added a lookup (7 lookups for six children), with the details shared only because the
gateway's one-hour coalescing was warm inside the test. After, the six children's full records add
no request. The dev call counts were not re-measured against a live campus.

**Still outstanding:**

- **The survey roster.** Followed since P234 (`cris_buildings._campus_candidates`): a survey counts as the site's
  roster when it names more than half of the site's buildings and has more than half of its own positioned buildings
  on the site, which keeps a town reconnaissance out. HRSH's 12SD00541 passes with 58 of 94. A survey that covers the
  site and as much again elsewhere still fails the second test, so its unpositioned buildings are not listed.
- **Per-pass caps, filled later since 2026-10-04.** One pass live-fetches at most `_MAX_SITE_DETAIL_FETCHES` (12)
  undetailed buildings, inside a 50 s budget under the task's 110 s soft limit, and asks REData's bulk queue to warm
  the rest. The row records what it left undetailed (`campus_pending`) and the radius it searched.
  `tasks.fill_cris_campus_details` then runs up to three more passes, 10, 20 and 25 minutes apart. Each does one lookup,
  adds every record REData has detailed since, and live-fetches up to the same cap. It changes nothing else in the row
  and leaves its age alone (`test_cris_campus_seeding.py::UndetailedCampusRecordsAreFilledLaterTests`). A campus
  REData has not warmed within an hour gains 36 more buildings, and the rest wait for the row's refetch. Not measured
  against HRSH's live 94 buildings.
- **A child the roster does not cover still fetches its own.** That is a footprint with no CRIS
  point inside it, a point more than 15 m from any, a building with no published position (a
  `linked_resources` stub), or any child of a site whose CRIS answer is empty (`{}` carries no
  search radius, so it cannot say which children its search covered). A seeded child's `district`
  is always its site's, where its own 200 m lookup would take the first site record it found.
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

## P36 — 43 BEM modifiers are applied in templates with no CSS rule, so intended visual states never render

`id: P36` · `status: open, awaiting Jess's rulings` · `updated: 2026-10-03`

**Jess, 2026-10-02: show her each one.** Make a screenshot list: each modifier, the element it is on, what it renders
as now, and what it was presumably meant to mark. Publish it as a decision page she can rule on (style it, or drop the
class). Nothing is styled or dropped before her ruling.

**The review page is up (2026-10-03): <https://claude.ai/artifact/EEcw3K7NH1FhU52w3qrE3b>.** Her choices land in its
database, collection `p36`, one document per modifier: `{modifier, choice: style|drop|other, notes}`. It has 41 entries:
`_KNOWN_UNSTYLED` holds 43, not 45, and the three `notif-item__icon-wrap--*` members are grouped into one. Each entry was
screenshotted on `development_main` with and without the class, and in 38 of them the computed styles match. Three
couldn't be screenshotted: the assistant needs the AI worker, the subscriptions page is staff-only, and the games need
the alpha entitlement. Evidence and capture scripts are in the session scratchpad (`p36/`). What the review found besides:

- **`map-overlay-btn--cancel` is a visible defect.** The boundary bar's Cancel label is rgb(247,247,247) on a white pill
  in the light theme. Clear and Done have their own colour rules; Cancel has none.
- **Notification icon colours:** only 8 of the 33 `NotificationType`s have a colour rule, and each applies only while
  unread. The templated `notif-item__icon-wrap--{{ n.notification_type }}` class is invisible to the check.
- **`visit-source--{{ visit.source }}`** is templated too, so the check doesn't see it. Its `manual`, `geolocation` and
  `safety_checkin` values have no rule.
- **`card--secondary`** counts as styled, but its only rule is scoped under `.child-building-detail`, so it does nothing
  on the pin page.
- **`btn--sel`:** an unused `.tag-view-btn--sel` rule looks like the class this button was meant to carry.
- **`org-bulk-btn--merge`** also carries a stray `btn` class, which gives Merge a shadow that Edit and Delete lack.

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

## P50 — `test_safety_chat` and `test_migration_0039_reverse` fail only under a randomized suite order

`id: P50` · `status: open` · `updated: 2026-10-04`

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

**A third order-dependent failure, found and fixed 2026-09-29** (`test_demo_seed_smoke`'s deferred-enqueue
test, reproducible with `-p no:randomly` after `test_queryset.py::FilterByCriteriaTagTests`). The cause was
production code: demo seeding `mock.patch`ed `celery.safely_enqueue_task`, which `bulk_followup` holds by
name, so the outcome depended on which test imported that module first. Seeding now uses
`suppressed_enqueues()` (a `ContextVar`). Worth checking first for the two above: process-global state set by
one test and read through a name another module imported earlier.

**A fourth, found and fixed 2026-10-04.** `test_oauth_client_provisioning.py::FirstPartyClientMigrationTests` read
the OAuth row migration 0010 made when the test database was built. In a mixed run with the migration tests, all
four failed with `Application.DoesNotExist`, and the file alone passed. They now run 0010's own
`create_first_party_client` over its historical models in `setUp`. Rows made at database setup are the third shape to
check for.

**Probed again 2026-10-04, did not reproduce.** Three shuffled runs (`-p randomly`, seeds 101, 202 and 303) of
both named files with 60 others picked at random: 1,020 tests each, no failure. Not a full-suite shuffle.

**A fifth, found and fixed 2026-10-04: not order but chance.** `test_export_import_completeness.py`'s
`ImportCustomFieldsTests::test_definition_and_pin_value_round_trip` lost its imported "Gatehouse" (`Pin.DoesNotExist`)
in one 22-file run and passed alone. Baker fills `Location.latitude`, a `DecimalField(max_digits=9,
decimal_places=6)`, with up to 99.999999: 96.46 in one sample of twelve. Since P282/P285 the pin import refuses an
off-globe coordinate, so about one run in ten dropped the pin. `core/tests/baker.py::SignalSafeBaker` now bakes a
latitude between 0 and 90 for every model (`core/tests/test_baker_coordinates.py`); longitude, 0 to 99.999999, is
already real and unchanged. Any other test that baked a location and checked its coordinate failed the same way at
random. Random data that a validator can refuse is the fourth shape to check for.

## P56 — `Cross-Origin-Embedder-Policy` is report-only pending one measurement; `require-corp` is ruled out

`id: P56` · `status: open` · `updated: 2026-10-02` · `corrects a "zero violation reports" claim measured with the wrong browser API; also corrects its own "overlays are the blocker" claim now that P159 downloads pasted overlay URLs instead of referencing them live - the blocker is P165 (third-party thumbnails) now`

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

**The open-ended host set that ruled `require-corp` out is gone; the tile vendors remain.** This
entry once blamed pasted map overlays (stored server-side since P159) and then third-party
thumbnails (served from this site's copies since P165) for keeping `img-src` at `https:`. Since
2026-10-02 `img-src` is an explicit allowlist (`settings/base.py`, held by
`test_csp_image_hosts.py`): the base-map tile vendors, the Maps JavaScript API's Google hosts and
the vendor CDNs. What `require-corp` would still break is the tile vendors in the table above,
which send no CORP and are requested without CORS. Not re-measured since 2026-09-05.

That points at `Cross-Origin-Embedder-Policy: credentialless` rather than `require-corp`:
credentialless sends no-cors subresource requests without credentials instead of demanding CORP, so
a vendor's tile still loads. ~~Evaluating it - and its browser support, which is narrower - is the
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

**Re-attempted 2026-10-02 on `development_main`, with the panels worker running: still not measurable there.**
The Street View fragment (`pin.street_view`) answers 204 for the e2e campus pin: no provider returned a slide,
so no embed iframe renders. And the configured public key (`UL_GOOGLE_PUBLIC_API_KEY`, 39 characters) gets a 403
from `https://www.google.com/maps/embed/v1/streetview` sent with this site's referrer, as a made-up key does. The
key is restricted or the Embed API is not enabled for it. Neither 403 carried a `Cross-Origin-Embedder-Policy`
header. If the 200 does not either, an enforced COEP blocks the embed (a nested document must send a compatible
COEP) unless the iframe carries Chrome's `credentialless` attribute, which loads it without the viewer's
cookies. The Embed API authenticates by key, not cookie. So the next attempt needs a key the Embed API accepts,
on a deployment that has one. It should check the 200's headers, and whether the embed still works with
`credentialless` on the iframe.

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

## P85 — Managers are typed, but `misc` stays off: it reports 478 lookup and plugin findings, and annotations do not survive a model-bound queryset's rows

`id: P85` · `status: open` · `updated: 2026-10-05` · supersedes "Every manager is a dynamic base class, so `Model.objects` is `Any` and 146 mypy errors are turned off to hide it"

**Fixed 2026-09-29: `Model.objects` is typed.** Every manager was declared
`class XManager(Base.from_queryset(XQuerySet))`. mypy cannot follow a call as a base class, so each
resolved to `Any`, and so did `Model.objects` and everything reached through it:
`x: int = Trip.objects` type-checked. Each manager is now

```python
_XManagerBase = Base.from_queryset(XQuerySet)


class XManager(_XManagerBase): ...
```

django-stubs' `get_dynamic_class_hook` builds a real `TypeInfo` for the assignment, and the class
statement keeps the runtime class importable under its own name, which is where the plugin looks for
it. At runtime nothing changes: `from_queryset` builds the same `BaseFromXQuerySet` class it built
inline, now bound to a name. The same probe now reports `TripManager[Trip]`; `.all()` is
`TripQuerySet`, `.first()` is `Trip | None`.

**Why not the bare assignment `XManager = Base.from_queryset(XQuerySet)` this entry used to
recommend.** The 2026-09-18 attempt converted the three abstract managers that way, got
`[django-manager-missing]` on reverse relations, and left open whether that was revealed or
introduced. Introduced. A bare assignment leaves the runtime class in `django.db.models.manager`
under a generated name, and the plugin maps it back only through its *base* manager's metadata,
which it finds by the base's runtime name. When that base is itself a bare `from_queryset` result it
lives in `django.db.models.manager` too, the lookup fails, `_default_manager` is never set, and every
reverse relation into the model is reported (`mypy_django_plugin/transformers/models.py`,
`AddManagers.get_dynamic_manager`). The subclass form never needs the mapping.

Supporting changes in the same work:

- 99 bare querysets parameterized with their model (`abstract.DashboardQuerySet["Trip"]`), as the
  base's docstring asks. `LinkQuerySet` and `AutoRemovalQuerySet` serve two models each and stay
  generic.
- The 38 managers with bodies bind their model (`class WikiManager(_WikiManagerBase["Wiki"])`), so
  `self.get_or_create()` inside them is a `Wiki`, not a type variable. The rest are filled per model
  by the plugin. `ProfileConnectionManager` passes its type parameter to `DashboardManager`.
- `objects: XManager = XManager()` annotations removed, abstract bases included: they pinned the
  manager to `XManager[Any]`. So were 24 hand-written `TYPE_CHECKING` reverse-manager declarations
  (`contacts: DjangoManager[SafetyCheckinContact]`), which hid the plugin's related managers and
  their queryset methods.
- `Trip`'s manager and queryset were built on the `Dashboard*` tier though `Trip` is a
  `PublicDashboardModel`; `SavedFilter`, `PushDevice`, `ProfileNote` and `NotificationLog` likewise
  under `FrontendDashboardModel`. Rebased onto the matching tier. `Trip.objects` gains
  `slug_or_uuid`; nothing called it on `Trip`.
- `ApiRateLimitManager` overrode `get_queryset` by hand; now the same pattern as the rest.

**What typing revealed: 83 errors in 48 files**, measured with the pattern applied and the querysets
parameterized, all fixed at the origin except the stubs limitations below. One changed what a user
sees:

- `services/trips/trip_ai_suggestions.py::_build_candidates` passed `requester_pin.slug` as
  `add_pin_slug`. A slug-less pin rendered `"None"` into the "Add to trip" button, which then failed
  with "That pin does not exist". Now `slug or str(uuid)`, which `trip_activities` already resolves;
  reproduced first (`test_slugless_requester_pin_is_addressed_by_uuid`).

One is a defect left alone. `controllers/site_admin.py` fills the admin stats "Top Locations" table
behind `hasattr(Location.objects, "annotate_pin_count")`, and no queryset has ever defined that
method (it predates the v0.2.0 history squash), so the table has always been empty. Showing it is a
behaviour change for Jess to decide; it carries a `TODO(P85)` and a scoped ignore.

The rest were types wrong or looser than the code, none changing behaviour: parameters narrower
than what callers pass (`trip_ids_for` receives an id; `attach_existing_comment_image` receives a
`TripComment`), `set[int]` returns that could hold `None` from the nullable `Place.domain_root`
(`granted_domain_ids`, `domain_ids_for_locations` now drop it, as every reader already treated it),
one variable reused for two types, `request.user` passed where only `User` is valid (narrowed with
`isinstance` as `controllers/two_factor.py` does), and nullable columns the query had already
excluded (guarded; the guard cannot fire).

**Stubs limitations, suppressed per line with the reason:**

- A queryset parameterized through an intermediate base (`PinQuerySet(PublicDashboardQuerySet["Pin"])`)
  is not generic: the plugin only rebinds one whose direct parent is `QuerySet`
  (`reparametrize_generic_class`, `bind_explicit_args`). Its rows are the plain model, so
  `.first()`, iteration and `.iterator()` drop `.annotate()` names that the queryset type still
  carries. Four sites (`import_export/export.py` ×2, `locations/enrichment.py`,
  `ai/tools/trips.py`). A PEP 696 type variable per queryset, defaulting to its model, should fix
  it; not attempted.
- `Distance` and `Area` are typed `float`; they return measures (`spotguessr/distance.py`,
  `places/resolution.py`).
- `Prefetch(to_attr=...)` (`export.py`); `Trip`'s declared `_eff_*` memo attributes read as a clash
  with annotations of the same name (`trips/queryset.py`); and a single `prefetch_related` mixing
  `Prefetch`es over different querysets, split into one call each (`controllers/memories.py`).

**What is left: `misc` is still disabled.** `--enable-error-code misc` reports 478 (2026-09-29); 494 on 2026-10-05,
the same kinds plus eight `except (*OBJECT_STORE_ERRORS, ...)` clauses, which mypy cannot read through a starred
variadic tuple. A 2026-10-05 pass over the 75 outside the two large kinds found no runtime fault:

- 249 `Incompatible type for lookup` - the lookup-value check the plugin could not run while
  `.objects` was `Any`. **Triaged 2026-09-29: no 500 among them.** 202 pass `request.user`
  (`User | AnonymousUser`) from a login-guarded view; django-stubs' fix is an `AuthenticatedHttpRequest`
  annotation, which narrows the parameter against `TemplateView.get`'s and so needs a decision on how views
  declare it. The other 47 are `pk=None`-able values whose `None` already means "not found"
  (`filter(pk=safe_int_or_none(...))`, the game invites' `get(pk=request.POST.get(...))` inside
  `except (DoesNotExist, ValueError, TypeError)`), server-written undo payload ids, floats into
  `DecimalField` lookups, `date`s into `__date__in` (a stubs gap), and a `values_list` union. The 2026-09-06
  sample that found two real 500s was taken before those were fixed; the category is loose typing now,
  not bugs.
- 155 `Cannot override class variable ... with instance variable` on `objects = XManager()`. New
  with this change and a plugin artifact: the plugin declares the manager a ClassVar on the base,
  and the subclass's plain assignment is checked against it. Annotating each as `ClassVar[...]`
  would pin a model inheriting an abstract base's manager to the abstract model, so it was not done.
- 16 the `EnrichmentSource` ClassVar pattern and 57 others of the benign kinds triaged 2026-09-14:
  annotation keywords, the self-referential M2M labels, deliberate `pk=None` lookups, mixins with no
  declared base, a shadowed enum member, `AppSettings.__getattr__`'s pydantic `super()`.

**Probe the whole tree.** A one-file invocation (`mypy src/urbanlens/dashboard/x.py`) left
`Trip.objects` as `Any` on 2026-09-29 against the same code the full run typed, so a single-file
probe proves nothing either way.

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

## P125 — This deployment's ceiling is between 500 and 1,000 concurrent users, and every wall it has hit so far was a container CPU limit: 175 on 2 app cores, 350 on 4, 500 on a 2-core database, and 1,000 on the same 4 app cores once the database was given 4 of its own

`id: P125` · `status: open` · `updated: 2026-10-06`

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

**Superseded as a statement about production: that was damballa's compose production, which stopped serving on
2026-10-02.** `urbanlens.org` moved to the k3s cluster that night, and compose's UrbanLens app and celery are stopped
(`../infrastructure/docs/STATUS.md`, "Production has run here since 2026-10-02"). Everything this section and the later
"production has none of this" bullets say about `production.sample.env`, `urbanlens_production_app` and
`urbanlens_production_db` is about that compose stack and is a record of what was true of it, not of production now.
As of 2026-09-21, the last time it was checked, none of the sample's sizing had been applied there: the container was
still uncapped (`docker inspect urbanlens_production_app` on damballa showed `NanoCpus: 0` on 2026-09-17, the same root
cause as P114's `CpuShares: 0`) and on the default 3 workers. Whether it was applied before the cutover was not
checked, and no capacity run was ever made against damballa. The sample file and its test still size the compose
files that dev and perf environments use.

**What production runs now** is the k3s web tier's own sizing, from the infrastructure repo's
`platform/urbanlens-app/base/deployment-web.yaml` on `origin/main`: 2 replicas, each `gunicorn -k gthread --threads 4`
with `WEB_CONCURRENCY=2` (16 request threads per site), a 100m CPU request, a 1Gi memory limit and no CPU limit, with
the database on CNPG. None of this is what the ladders above measured (4 cores, 6 to 12 workers, a 4-core Postgres
container), and **no capacity run exists against it**, so the 500-user ceiling above says nothing about production
today. Re-running `tests/perf/k6/population.js` against a k3s-shaped environment is what would.

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

- **The 4-core compose override was never measured on production, and production is no longer compose**;
  see the superseded note in the 2026-09-17 section. Everything above is 2 cores.
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
- **Production, then compose, had none of this, and is now k3s.** `CPU_LIMIT__APP=4`, `WEB_CONCURRENCY=6` and
  `MEM_LIMIT__APP=3g` went into `production.sample.env` and were deployed to the perf environment only; on 2026-09-21
  damballa's production app container was still uncapped (`NanoCpus: 0`) and on the default 3 workers. What production
  runs now is described in the 2026-09-17 section's superseded note.
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
- **Production, then compose, had none of this.** `production.sample.env` carries the app tier *and* the
  database sizing, and was deployed to the perf environment only. damballa's `urbanlens_production_db` ran bare
  `postgres` with no arguments and `NanoCpus=0` (verified read-only 2026-09-21), so it had neither the limits nor the
  `shared_buffers`/`jit=off` tuning. That database is the retry path since the 2026-10-02 cutover, not production's;
  production's is CNPG on k3s, whose tuning this entry has not read.
- **The 30-minute window is a real trade.** A revocation that leaves the session record intact - an
  admin disabling an account, a password change invalidating other sessions - keeps drawing tiles,
  and nothing else, until the entry expires. Signing out, a flush or an expiry revokes immediately,
  because the gate re-reads the session every time.

## P132 — A global search read the whole site's rows to answer one viewer's question; semi-join probes cut its SQL 60%, and the ceiling moved off the database

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

## P134 — The app tier, not the database, is what runs out; caching the navbar's access question moved 500 concurrent users from six budget breaches to none, confirmed by a second ladder on the released tree

`id: P134` · `status: open` · `updated: 2026-09-22`

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

## P131 — REData's ~1.45s PBKDF2 key-check is fixed upstream (confirmed 2026-09-21); basemap tiles now pay 0.43–0.98s for cold-tile rendering instead, and the concurrency bound's own trigger condition is met for auth but not for that

`id: P131` · `status: open` · `updated: 2026-10-06` · `supersedes the 2026-09-19 "~1.45s PBKDF2 per call" claim below: REData shipped a hasher change (`f87d0ebe`, 2026-09-19) that removed it. Left open because the entry's own open items were about what to do once that happened, and that work is now live, not because the original defect is still present.

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
first use, so nothing had to be reissued. The change is `f87d0ebe`, first tagged v0.3.4. (This entry
used to cite REData's `T9` as the hasher change; REData's `T9` is the `style_url` request that `167cd8c9`
answers, a different piece of work, described just below.)

**What replaced it as the cost that matters here: rendering a tile REData has not served before.**
0.43s to 0.98s cold, ~0.11s warm, on the three raster layers measured above. `street` has no row
because production then did not serve it as tiles at all: `GET /api/v1/tiles/street/14/4823/6037/`
answered `400 vector_layer_not_served` in 0.10s, and the catalogue published `street` and `dark` as
`source_type: "vector"` with a `style_url` and **no** `url_template`. That has since changed: REData's
`167cd8c9` (2026-09-21) publishes `url_template` and `style_url` both and removes `vector_layer_not_served`
(its `api/views_tiles.py` on `origin/main`, `TileSourcesView`), and it first shipped in v0.3.4, in production
since 2026-10-06 15:21Z. So `street` and `dark` have a raster row to measure again, which this entry has
not done; the 0.43–0.98s figures are for `terrain`, `satellite` and `borders` only. What they measure is tile
rendering/caching inside REData, not authentication, and — like the old PBKDF2 cost — it does not
depend on a warm cache existing, so a first viewer of any given tile still pays it. Not re-measured:
whether it is CPU-bound rendering, an upstream fetch, or something else; this entry only has REData's
black-box timing, not a profile of its cause.

**What this changes for UrbanLens.** The fan-out shape is unchanged — a cold map viewport is still
~30 upstream requests, one page view — but the per-request cost dropped from ~1.45s to, on this
measurement, 0.43–0.98s for a genuinely uncached tile and ~0.11–0.13s for one REData has already
rendered. `basemap_tile_upstream_concurrency` (default 6, `src/urbanlens/UrbanLens/settings/app.py:751`; it was 2
when this was measured, and 6 has shipped on production since 0.8.0, `ed9ab3608`) still exists to keep that fan-out
from occupying every gunicorn request thread in a process (`--threads 4`, `package.json`'s `start` script) and
queuing the rest of the site behind a cold map load; the setting's own description now says that at or above the
thread count the thread pool rations instead, and that the default sits above it on purpose. Nothing in this
session's measurement removes the need for some bound, only changes what number it should be.

**Open:**

- ~~The hasher change in REData.~~ **Done.** Shipped and confirmed above; this was the entry's
  original "actual fix" and is no longer open.
- **What `basemap_tile_upstream_concurrency` should be, now that the trigger condition is partly
  met.** (It has been raised to 6 since this was measured, by a change that did not record a new measurement;
  the paragraph below still describes what would justify any number.) This entry's own text set the trigger as "if TTFB for a valid key matches the 55ms an
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

## P144 — Every UrbanLens environment shares one REData key and its 1,000/hour lookup budget, and REData has no way to exempt production

`id: P144` · `status: open` · `updated: 2026-09-24`

Read from REData `main` (`src/redata/api/throttling.py`, `settings/base.py`) and
checked against production. The deployed `throttling.py` and `ApiKey` model
hash-match `main`. Key identities were compared by the `rdk_` prefix and REData
row pk only.

**The limits.** REData throttles per `ApiKey.pk`, in DRF throttle classes:

- `ApiKeyRateThrottle`: 2,000/hour on every endpoint.
- `ApiKeyLookupThrottle`: **1,000/hour**, stacked on the 2,000. It applies to
  about 60 endpoints that can start a live fetch, including `parcels/lookup`,
  `places/*` (not `places/cid/`), `search/web`, `street-view/*`, `imagery/*`,
  every near-point domain, `reference-documents/*`, cultural-resource lookup,
  fetch and attachment download/extract, and `points-of-interest/lookup`.
- `capabilities/`, `parks/nearby/`, `maps/` and parcel sub-resources (buildings,
  boundaries and the like) are on the 2,000 only.
- Tiles have their own 20,000/hour, which replaces the default.
- The rates are hard-coded in `DEFAULT_THROTTLE_RATES`, with no env override.
- A throttled request is `429` with `Retry-After`; the body says "Expected
  available in N seconds".
- DRF runs every throttle on every request, so a request the lookup throttle
  refuses is still charged to the 2,000.

**Which key each environment uses** (`UL_REDATA_API_KEY`):

| environment | key | REData row |
|---|---|---|
| local dev (`development_main`) | `rdk_WwVl…` | pk 2 |
| damballa production | `rdk_DgqZ…` | pk 1 |
| damballa staging | `rdk_X1iD…` (since 2026-09-24) | staging's own |
| k3s `urbanlens` | `rdk_DgqZ…` | pk 1 |
| k3s `urbanlens-staging` | `rdk_X1iD…` (since 2026-09-24) | staging's own |

So HRSH runs on chiron spend only the dev key's budget. Until 2026-09-24 **production shared one
lookup pool with damballa staging and both k3s namespaces**, so a staging test run could throttle
production. Both staging stacks now use `UL_REDATA_STAGING_API_KEY` (damballa's staging `.env`, and
infrastructure `bcd17ff`'s SOPS secret for k3s), so only production and k3s `urbanlens` share pk 1.
What remains open is the service tier below.

**Production is not exempt.** `ApiKey` has `user, name, prefix, key_hash, scopes,
last_used_at, revoked_at`, and `scopes` controls permissions only. Nothing in
`throttling.py` reads anything on the key except `pk`. Being owned by a superuser
exempts nothing.

**What REData would need (not changed from here):**

1. A field on `ApiKey`: a `throttle_tier` (`standard` / `service`), or
   `rate_overrides` (JSON, scope to rate), with a migration.
2. `_ApiKeyThrottleBase` reading it:
   - For exemption, return `None` from `get_cache_key` for a service-tier key;
     DRF treats `None` as "do not throttle".
   - For per-key rates, override `allow_request` to set `self.rate` from the key
     and re-run `parse_rate` before `super()`. `SimpleRateThrottle` fixes its rate
     in `__init__`, before the key is known.
3. The field on the dashboard's API-key page and in the admin, and tests in
   `api/tests/test_throttling.py`.

**What UrbanLens should do regardless.** Give production its own key, so staging
and k3s cannot spend its budget. `.env` on chiron has an unused
`UL_REDATA_PROD_API_KEY`. This is operational, not code.

UrbanLens's side of the volume is X30. A shared breaker now stops calling
a pool once REData throttles it. Identical asks are coalesced. A building pin
takes its site's answer for site-level panels. None of that removes the need for
a service tier: at the ~50 calls a building page view cost before the fix, 1,000 an
hour was about 20 page views.

## P145 — The HRSH courtyard pin on staging got a circle, a service road for a title, a building's name as an alias, no Wikipedia article and one building in its CRIS card

`id: P145` · `status: open` · `updated: 2026-10-06` · `decision: D20` · `tests: tests/integration/specs/location/hrsh-naming.spec.ts`

Jess pinned 41.73266, -73.92736, a courtyard on the Hudson River State Hospital campus, on staging
(Location 67). Traced read-only on staging, then reproduced and fixed on `development_main`. **The fixes shipped
in v0.8.0, which is what production runs. Open until the caches from before the fix refresh and the forced boundary
run below is done.**

The commits cited in the table were `development_main` commits and are not reachable from `origin/main` or
`release/v_0_9_0`: v0.8.0 landed as one squash, `ed9ab3608`. Their changes are inside it, checked on 2026-10-06 by
looking for each commit's added source lines on `origin/main`; `8799603ca`'s `surveyed_buildings` helper has since
been reshaped (the `HistoricName` fallback it added is at `plugins/builtin/cris_buildings.py:822-823`). Search the
squash by the commit's subject, not its hash.

| Symptom | Cause | Fix |
|---|---|---|
| Default circle, not the parcel | REData `parcels/lookup` was refused (the Dutchess County ArcGIS budget was spent) and the miss was stamped as final. Overpass's `around:100` missed the OSM campus polygon (way 889025160, 98% of the tax parcel) because its edges are 130–205 m from the point | `4f9a390fd`: a transient REData refusal defers the provider and is retried with backoff, never stamped. Overpass also asks for named areas containing the point (`is_in`), capped at 5 km², never zoning |
| Wiki titled "Courtyard Drive" | Nominatim's reverse geocode named the smallest object under the point, a private service road (way/352353227). `nominatim` is first in the default name priority, so it won; the same name became `Location.official_name`, which every search (Wikipedia, Wikimedia, Smithsonian, GDELT) used | `b08a367fe`, `3146ed80d`: the D20 tier metric. A road never names a place; a road name already in place is retired |
| "BLDG 45/MORTUARY & LAB (1896)" as an alias of the parcel | The nearest CRIS building's name was offered as a parcel name | `b08a367fe`: a building names a parcel only when the parcel holds that one building; inadmissible automatic aliases are pruned |
| No Wikipedia article | The miss was cached for the query "Courtyard Drive (Fairview)". The article's coordinates (41.73306, -73.92833) are inside the parcel | `d0ad746fb`: a geosearch hit placed inside the parcel matches; a cached miss is re-asked when a parcel arrives |
| CRIS card showed one building | The card showed the nearest building record, not the listing | `8799603ca`: at site scope the card is headed by the National Register listing, with its NR number and the surveyed buildings |
| No building child pins at the courtyard | The fallback roster had no CRIS buildings, and one building never got a child pin | `ec91970f2`, `262d502ba`: CRIS building points join the OSM roster by REData's reconciliation rules; a single building gets a child pin too |
| No city, state or country | Google could not address the point | `dbb781267`: the administrative fields come from OpenStreetMap; the street never does |
| Second root pin kept a lesser name | Its Location never fetched the register listing | `78e79922c`: it inherits the property wiki's register, article and site titles |

**Containment is what admits a register name.** At the requirement pin, REData's National Register answer
was only "Roosevelt, Isaac, House", a point listing about 550 m away. Both HRSH points lie in one tax
parcel (3532 North Rd, about 473,000 m²) and one NR listing (94NR00622); 42 CRIS buildings are inside
the parcel.

**Verified 2026-09-23** on `development_main`: `hrsh-naming.spec.ts` passed at both points with
`UL_E2E_HRSH_FRESH=1`, then again at `78e79922c` alongside `hrsh-wiki`, `hrsh-boundary`, `hrsh-child-pins`
and `hrsh-place-identity` (46/46).

**Open questions for Jess:**

- The campus is titled "Hudson River State Hospital, Main Building", because the NR listing is the Main
  Building's even though its boundary covers most of the campus. D20 asks whether a structure-scope listing
  should rank below Wikipedia on a multi-building parcel.
- A property with one known building now gets one building child pin. That applies to every house, not only
  campuses.

**After deploy:** register, CRIS and parcel-building caches from before the fix carry no
`contains_point` and name nothing until they refresh. Whether staging runs v0.8.0 yet was not checked here. The miss stamped on Location 67 needs a forced boundary
run (`generate_boundaries_for_location(67, force=True)`), which nobody has done from here.

## P148 — A county-sized "parcel" put strangers across the Capital District into one wiki and pin-in-common domain

`id: P148` · `status: open` · `updated: 2026-10-06` · `tests: src/urbanlens/dashboard/tests/hypothesis/test_oversized_places.py, src/urbanlens/dashboard/tests/hypothesis/test_redata_boundary_provider.py`

On `development_main`, parcel place 1740 ("103 Schermerhorn Rd, Cohoes") covered 1,322 km² and held 105
Locations from Duanesburg to Cohoes. Place membership decides wiki visibility (`_domains_given_pins`) and
"pins in common" (`pins_sharing_a_place_with`), so `e2e-primary` and `e2e-secondary` counted as sharing a pin.
Seven more parcels were the same kind: 856 (1,866 km²), 864 (227), 1652 (121), 1681 (113), 72 (103,
HRSH's old outline), 32 (70), and 877 (13.8). All are convex with 7-14 vertices.

**It was a hull of REData buildings, not a union, a county layer or a merge chain.** For a NY parcel
`parcel_geometry` is always null. `RedataBoundaryProvider` then asks `/parcels/{uuid}/boundaries/` and, when
that yields nothing, takes the convex hull of every building record not flagged `is_on_property: false`.
REData flags CRIS survey-roster and consultation-project matches as on-property with no distance check.
For 19 Schultz Rd, one house lot, it returned 115 such records at a median of 5.2 km and up to 28 km.
Their hull is 332 km². REData places a parcel's assessor building at the queried coordinate (it did for
99913's parcel), which is why place 856's outline has a vertex exactly on its triggering pin (98318).

**Place 1740, 02:19 on 2026-09-24, just after a container restart.** Location 99913 was created at 02:17:56.
`celery_worker` ran `generate_location_boundaries` for it, then `ensure_place_outcome`, then
`provision_outcome_for_coordinate`, then `RedataBoundaryProvider`. The parcel lookup resolved uuid
`781dd879`. At 02:18:31 the ranking call for that uuid timed out (read timeout 30 s, logged at DEBUG only),
so the provider fell through to the hull. At 02:19:10, `upsert_place` created 1740 and Boundary 347
(`source=redata`). `resolve_locations_in` then moved 104 locations onto it. `reconcile_wiki_nesting` nested
dozens of placeless wikis under wikis 3422 and 3423, both of which stood on it. REData now answers the same
uuid with one building and a 0.001 km² suggested boundary, so the exact list it returned then cannot be
recovered.

**Fixed in `356c2ca6a` and `178cc8939`**, `development_main` commits that are not reachable from either branch: both
are inside the v0.8.0 squash, `ed9ab3608`, which production runs (`MAX_PLAUSIBLE_AREA_SQM` is at
`models/place/model.py:37` and `PlaceQuerySet.implausible` at `models/place/queryset.py:44` on `origin/main`).
- A transient ranking or buildings failure defers the provider. It no longer degrades to the hull.
- The hull uses only records on the property (REData's `on_parcel` is not false) within 1 km of the queried
  point, and never a `project` match.
- A 10 km² ceiling for parcels and sites, and 1 km² for buildings (`MAX_PLAUSIBLE_AREA_SQM`), is enforced
  in the provider chain and in `upsert_place`.
- `PlaceQuerySet.implausible()` is excluded from `resolvable()`. Such domains are dropped from
  `_domains_given_pins`, and pins on them match only their exact Location in `common_pins`.
- `looks_like_a_split` is never true for an implausible parcel. Otherwise correcting one would grandfather
  every holder into every successor.

**`178cc8939`, from adversarial review.** Pre-ceiling Boundary candidate rows (1740's Boundary 347 among
them) are still in the table, and `apply_winning_boundary` re-applies the vote winner on every refresh of a
voted place. `boundary_options` now drops implausible candidates, so none can win. `wiki_property_polygon`
returns None for an implausible outline, so a placeless wiki on one nests nothing. `area_sqm` returns infinity
instead of raising when PROJ cannot project a geometry.

**Dev data repaired** with `manage.py detach_oversized_places`: 8 places, 564 locations, 1,243 child
places detached, 990 wikis un-nested (1 re-nested by reconciliation). Afterwards `_have_common_pin` is false
both ways for the e2e accounts. A second run finds nothing. 205 locations are left unplaced with
`place_resolved_at` cleared, so the chain places them again when they are next viewed. The before-image
(places, locations, wikis, pin types) was saved outside the repo.

**Still open:**
- **Production is unchecked, and now runs the fix.** The hull fallback shipped in v0.6.0 and the ranking call in
  v0.7.0; before v0.7.0, every NY parcel took the hull directly. A read-only check lists
  `dashboard_places` rows with `kind in ('parcel','site') and area_sqm > 1e7` (or `kind='building' and
  area_sqm > 1e6`). For each, count the distinct `dashboard_locations` and `dashboard_user_pins.profile_id`
  under its `domain_root_id`. Hulls below the ceiling look like 877: `ST_NPoints` under about 20, convex
  (`ST_Area(ST_ConvexHull(g)) / ST_Area(g)` ≈ 1), a `redata` Boundary row, and members more than 1 km apart.
  The fix is deployed with 0.8.0, so `detach_oversized_places --dry-run` (on `origin/main`) gives the same list;
  nobody has run either against production.
- **A hull below the ceiling can still exist.** 877 (13.8 km²) is only caught because it is over 10 km²;
  hulls of 8.4 km² (23, superseded) and 4.9 km² (1632) remain. The origin fix stops new ones.
- **Children the bogus parcels spawned are detached, not deleted.** 864's 500 OSM building places (an
  Overpass fetch of 5,788 buildings inside its outline) and the 359 building child pins made for
  `e2e-primary` under it are still there. 72's 743 REData building places came from the statewide
  survey-roster expansion REData fixed in `fb878e2c` (first tagged v0.3.4, in production since 2026-10-06). Their 478 wikis are no longer nested under the HRSH wiki.
- REData still flags roster and project matches 28 km away as `is_on_property`. That belongs in REData.
- Oversized `Boundary` candidate rows were left in place: they can no longer win, and deleting them would
  also delete any votes cast on them.
- `wiki_merge`'s lineage path (`place__parent_id`, `ancestors_of`) has no plausibility check of its own. It
  relies on no implausible place having children, which the ceiling and the repair ensure.

## P165 — Articles saved before 2026-09-30 name provider images in their source until `manage.py localize_article_images` runs on each deployment

`id: P165` · `status: open, handed to infrastructure` · `updated: 2026-10-02` · `was "Third-party thumbnails load directly from provider hosts, leaking every viewer's IP and referrer"; every code item is done, and what remains is an ops step`

**Jess, 2026-10-02: run it now, without waiting for v0.9.0.** The command ships in v0.8.0, which production runs. This
repo can't reach production, so the run went to the infrastructure repo (N33,
`docs/handoffs/infrastructure-jess-decisions-2026-10-02.md`): `--dry-run`, then the real run, on staging then
production. Close this when they report the counts.

**Ruled by Jess 2026-09-30:** download each thumbnail and cache it locally forever, served by UrbanLens, with a record of where it came from. That also covers a provider taking an asset offline. This is a different question from browser-direct geocoding (D25).

**What remains: run `manage.py localize_article_images` on every deployment** (`--dry-run` first prints how
many articles would change). Each changed article gets a new revision, "Images stored on this site". Run on the
development_main dev stack 2026-10-02 (66 articles changed); not known to have run on staging or production. Until it runs, an article saved before 2026-09-30 still names its images by the provider's
address in its source. Its rendering is already local, but the editor canvas, which is how every viewer sees an
article, loads the source, and since 2026-10-02 `img-src` refuses those hosts: those images show broken in the
editor until the command runs, rather than loading from the provider. So run it as part of the deploy that ships
the `img-src` change. Wikipedia-seeded wiki articles are the likely bulk (not counted); a seeded Old State House
article was measured hotlinking `thumb.wikimedia.org`, which answered 403 anyway.

**What was done.** `services/media/remote_copies.py` keeps a `RemoteImageCopy` row per remote image (source URL,
provider, provider page, checksum, fetch time) and the page links `media-copy/<digest>/`; the first request
downloads the source and the sandbox worker re-encodes and keeps it (`docs/MEDIA_PIPELINE.md`, "Media previews").
Every gallery, panel, search, picker, favicon, Gravatar preview, satellite and street-view image is served from a
copy, and so is every image in an article: saving and the command rewrite the source
(`services/wiki/articles.localize_article_images`), and rendering rewrites what it shows. Since 2026-10-02 the
source rewrite covers reference-style images (`![a][ref]`, whose `[ref]: address` definition is rewritten) and
addresses with parentheses, and keys each copy by the address rendering keys it by, so both find one copy.

`img-src` is an explicit allowlist since 2026-10-02 (`settings/base.py`): the tile vendors in
`frontend/ts/shared/map-layers.ts`'s `TILE_DEFS`, the Google hosts the Maps JavaScript API needs for
SpotGuessr's Street View, and `unpkg.com`/`cdnjs.cloudflare.com` for images vendor stylesheets draw from beside
themselves (leaflet.draw's toolbar sprite), plus a configured media origin, vendor mirror and vector-basemap
style origins. `test_csp_image_hosts.py` reads every tile template out of the page code and fails when one's host
is refused. Leaflet's marker images are served from this site's static files.

**Left to rendering, and refused by `img-src` in the editor** (the source rewrite does not find them; how many
real articles hold any was not counted):

- a definition shared by an image and a link is rewritten, so that link then opens the copy;
- an `<img>` with a `>` inside a quoted attribute, a definition whose address continues past a list item's indent,
  and image syntax glued to a bare URL, which linkify swallows;
- an image inside a heading changes that heading's anchor when it is rewritten, as it already did before.

**Also now refused:** an `Image` row whose file never landed falls back to its `source_url`
(`Image.display_url`/`thumb_url`), which is a provider's address, often its page rather than a picture. It used
to load from the provider; now it shows as missing. Not counted on any deployment.

**Checked in a browser 2026-10-02**, on the dev stack: the map, a pin page and a wiki page load every tile and
Leaflet's markers from this site with no CSP violation. Before the command ran, the one violation was a Wikipedia
image in the article editor's canvas, as described above. `connect-src` now also admits `tile.openweathermap.org`,
which MapLibre fetches for the weather overlay (`test_csp_image_hosts.py` reads MapLibre's tile templates too), and no
longer lists OpenTopoMap, which no page code loads. **Not checked:** SpotGuessr's Street View; its Google hosts are
Google's documented list, not observed traffic.

## P167 — Upstream-bound tasks with four-minute limits share the interactive worker's four slots with safety alerts and signup mail

`id: P167` · `status: open` · `updated: 2026-09-29`

**Deferred by Jess 2026-09-30.**

D13 says the INTERACTIVE queue never holds anything that can run for minutes, but `enrich_wiki_location`,
`generate_boundaries_for_location`, `prefetch_location_external_data`, `cache_media_item_into_album`/`_wiki`,
`run_link_extraction`, `fetch_recorded_weather[_at]`, `classify_detail_marker`, `generate_image_keywords` and
`build_map_document` carry 210-270 s hard limits there. `celery-worker` runs `-Q interactive` at concurrency 4.
Measured 2026-09-24 during a Playwright run on the dev stack: every new pin's wiki queued enrichment onto
it, the queue stood 34 deep behind one consumer, and trip-invitation delivery and wiki auto-creation missed
their 60 s and 2 min waits.

Moving the upstream fetches to `panel_fetch` (tried and reverted 2026-09-28, 7443b573d / ddc0ffec6) does not
work as that worker is built: `--pool=threads --concurrency=20` in one process under 1 GiB, in compose, k3s
and production alike. Twenty enrichment, boundary and media-caching tasks at once reached 1.17 GB in the
main process within 25 s and the dev panel worker was killed and restarted 12 times, taking every panel fetch
down with it.

A fix needs a worker sized for memory-heavy upstream work: its own queue (for example `enrichment`) on a
prefork pool with modest concurrency and `--max-memory-per-child`, added to compose, the k3s manifests and
production together, so no deployment has a queue nobody drains. That is an infrastructure change across the
sibling repo, not made here. `test_interactive_queue_stays_short.py` (in the reverted commit) is a ready-made
guard for the invariant once the worker exists.

**Rejected 2026-09-29: routing them to `bulk` instead.** It needs no infrastructure change, because every
deployment already drains `bulk` (k3s's single worker takes every queue). But `celery-worker-bulk`'s two prefork
slots also run `maintenance`, including `drain_task_outbox` and the stalled-work requeues, so a large import that
creates hundreds of wikis would starve those sweeps for hours: the same failure moved to a different worker. The
dedicated worker also needs a database role. Each worker class logs in as its own role with a connection limit
sized to its pool (`docs/notes/database-roles.md`: `ul_worker` 5, `ul_bulk` 4, `ul_panels` 21), so a new container
on an existing role would exceed that role's limit. The fix is a new role in the db-setup convergence, a compose
service, and a k3s deployment, landing in that order.

Reverting the routing does not empty the queue: messages already on `panel_fetch` keep their task names, so the
worker kept OOMing on 258 of them until they were dropped by hand. Any environment that ran the move needs that drain.

The panel worker also OOMs on a backlog of its own tasks, cause not yet identified. With only `fetch_panel_source`
left (366 queued by the crash loop), it reached 1 GiB within 15 s of each start, four more times, while 15 or fewer
messages were unacked; idle it sits at 236 MiB and a full Playwright run kept it near 240 MiB. Per-call growth
measured in isolation does not explain it: `satellite`, `street_view` and `boundary` add 90-120 MiB on the first
call in a process and 0 on the next three, so that is import cost, paid once per worker. Suspects: one pin whose
fetch is huge and is redelivered after every kill, or many large imagery bodies held at once.

## P170 — Nothing deletes article revisions, and each one is a full copy of the article

`id: P170` · `status: open` · `updated: 2026-09-29` · `found by: N29 batch 33, re-verified 2026-09-29`

**Ruled by Jess 2026-09-30:** keep every revision, and store them as diffs rather than copies. Do not start until the earlier discussion of this, which involves concealed users, has been found and understood.

`services/wiki/articles.py` writes an `ArticleRevision` holding the whole article body on every save, and
no task in `tasks.py` or the beat schedule removes one. An article edited often grows its history by its
full length each time. The conflict-locking half of N29's finding is fixed (`test_article_conflict_locking.py`);
retention is not.

Needs a retention decision before code: how many revisions, or how old, an article keeps, and whether a
revision some other record cites (a revert, a moderation note) is exempt. Storing diffs instead of copies
is the other lever, and changes what a revert has to do.

## P182 — A building place from an OSM relation has no outline, because REData sends the relation's centre point; containment can never reach it

`id: P182` · `status: open, fixed upstream and deployed, live check pending` · `updated: 2026-10-06` · `found by: P181's investigation, 2026-10-01`

`BuildingNester` (`services/pins/pin_restructure.py`, via `ensure_building_places`) creates a building place for
each building record it mirrors. For HRSH's Kirkbride (OSM relation 10813427) on v080e2e that place, 483, was stored
with no geometry, although the relation is a multipolygon. Nothing can be resolved onto a place with no outline:
a marker at the building's point stands on the parcel instead, and the building's outline is never drawn.

The sweep used to hide this by pointing the Location at the building place directly (`attach_location`). That was
removed for P181 because the Location is shared; the owner's building pin and the building's wiki still read as
buildings (`places/scope.implied_pin_type`), and the wiki keeps its own `place`.

**Cause: REData sent no outline.** The `parcel_buildings` record for `overpass:relation/10813427` has a Point
`geometry` (the relation's centre) and no `residual_geometry`, though its source's attributes say
`type: multipolygon`. Every Overture record in the same answer, and 17 of the CRIS ones, carry polygons; it is
the only Overpass record. `building_footprint` and `_as_multipolygon` handle polygons and multipolygons, so
UrbanLens stores what it was given. Asked of REData in
[`handoffs/redata-osm-relation-building-returned-as-point.md`](handoffs/redata-osm-relation-building-returned-as-point.md).

**Upstream fix, deployed.** REData joins a relation's split member ways into rings (`c1e50276`, 2026-10-01) and replied
(its T10) that Kirkbride should come back as a polygon, probably merged with its Overture footprint. `c1e50276` is
inside `5aabe887`, the build production ran until 2026-10-06, and inside v0.3.4, which has run there since 2026-10-06
15:21Z. On REData staging HRSH's MAIN/ADMIN came back a Polygon, per
[`handoffs/redata-osm-relation-building-returned-as-point.md`](handoffs/redata-osm-relation-building-returned-as-point.md);
that was not re-read against production. Nothing here should need to change: `upsert_place` updates a building place by
its provider key once the cached `parcel_buildings` answer refreshes. If the merge gives Kirkbride a new key, place 483
is orphaned, with no Location on it: migration 0034 moved them all by containment. To close: refresh HRSH's buildings
against production and re-run the location project. Neither has been done, so this stays open.

## P206 — `dashboard_location_cache` is 81% of production's database

`id: P206` · `status: open` · `updated: 2026-10-04` · `found by: infrastructure's 0.8.0 deploy findings, item 9`

1,915 MB of 2,361 MB: 135k rows, about 1.85 GB of it TOAST, with a 69% TOAST hit ratio against 99.9% for heap. Index
scans fetched 972k tuples from it in a day, and one pooler pod sent 11.35 GB to clients in five hours, most likely from
it. It also sets the nightly dump's size and its 12 minutes. Questions: which reads pull the whole `payload` where a few
fields would do (`.only()`/`defer()`, or a JSON path), and does anything prune expired rows?

Measured on `development_main`, 2026-10-03:

- **Nothing prunes the table.** There's no task, beat entry or command for it; the deletes in `migrations/` are one-off.
  A row past `external_data_cache_days` is refetched on its next read, and otherwise stays forever.
- **Dev can't reproduce production's size.** Dev has 10,174 rows in 6.6 MB, about 650 bytes a row. Production
  averages about 14 KB a row. The largest dev sources are `hazard_history` (402 kB over 109 rows), `epa_echo` (max row
  46 kB) and `parcel_buildings` (max row 57 kB).
- **Next:** production's per-source row count and `sum(pg_column_size(data))` (the query this used is in this entry's
  history), and the infrastructure side's `pg_stat_statements` output, which names the queries that read it.

**A prune is not free (2026-10-04).** A row past its age is also a record that a fetch was tried. The backfill
enrichment sources (`services/locations/enrichment.py`) pick locations by `~Exists(LocationCache …)` of any age, and
`wikipedia._backfill_street_address` skips when its marker row exists. Deleting expired rows would queue every pruned
location for those upstream calls again. `wiki_seed`, `nps` and `epa_echo` also read rows regardless of age. A prune
has to keep marker rows, or strip `data` while keeping the row, or those gates have to learn an age.

## P210 — Pin-share notifications stored before 2026-10-02 still name the sender's own pin

`id: P210` · `status: open` · `updated: 2026-10-02` · `found by: P193's share-consent tests, 2026-10-02`

Until P193's fix, `create_pin_share` wrote the recipient's notification as `"{sender} shared {pin.display_label} with
you."`: the sender's own name for the pin, or its address when it had none, which a share without `shared_name` never
consents to pass on. New notifications use `PinShare.safe_place_label`. Rows already stored keep the old text, and the
inbox and notification history render `NotificationLog.message` as stored
(`partials/notifications/notification_item.html:16`), so a recipient still reads it. Copies already sent by email or
text cannot be recalled. How many rows exist was not counted.

The fix is a data migration rewriting `message` on `notification_type=PIN_SHARED` rows that still have a `pin_share`,
built the way `create_pin_share` builds it now (sender name through `resolve_visible_identity`, then the child-pin and
already-pinned suffixes). Not done: it rewrites stored rows users see, which wants Jess's say-so.

## P240 — Inside the US, Overture data needs REData's index-backed lookups, which production now runs (v0.3.4); only the buildings route has been seen answering

`id: P240` · `status: open` · `updated: 2026-10-06` · `follows: P110`

Since P110 (archived 2026-10-05), every Overture question REData's mirror covers goes to REData: buildings to
`GET /buildings/?provider=overture&radius_meters=10`, places to
`GET /points-of-interest/lookup/?provider=overture&radius_meters=150`
(`services.apis.locations.boundaries.overture.OvertureProvider`). Probed once each against the REData then deployed
(5aabe887) on 2026-10-03, at the US Capitol: `/buildings/` gave no response within 60 s, and the places lookup was a
504 from REData's proxy at 90 s. `/capabilities/` answered in 0.3 s, so REData was up.

The cause was on REData's side. Both lookups filtered with `geometry__distance_lte` on SRID 4326 columns, which
compiles to `ST_DistanceSphere(...) <= r`, cannot use the spatial index, and scans the whole US table. REData
replaced it with an index-backed `ST_DWithin` prefilter (`7ac19bf6`, `bfb47503`, on its `release/0.3.0` branch; REData
checked the plan with `EXPLAIN`), first tagged v0.3.4, which has been in production since 2026-10-06 15:21Z. The same
release syncs `roof_shape`, `roof_material` and places' `operating_status` into `attributes`, where UrbanLens already
reads them, and takes a place's category from Overture's `taxonomy` now that release 2026-09-23 dropped `categories`.
It does not backfill `buildings:read`: this entry used to say REData's migration `0007` did, but `0007`
(`api/migrations/0007_backfill_locations_prewarm_scope.py`) backfills `locations:prewarm`, and `buildings:read` appears
in it only in the frozen list of scopes a full-grant key already holds. No REData migration grants `buildings:read`,
so whether an existing key holds it is a fact about that key.

Seen live on 2026-10-06, by the session that coordinated this edit and not repeated here: production's key 1 answered
`GET /api/v1/buildings/` with 200 and 40 Overture results.

What it cost while production REData predated these lookups (until 2026-10-06):

- Every US Building Characteristics fetch waited 30 s (`redata_context_gateway._REQUEST_TIMEOUT`), raised an outage,
  and cached nothing, so the panel stayed empty and was retried.
- The chain's Overture step deferred after the same 30 s, which scheduled up to `MAX_DEFERRED_RETRIES` reruns of the
  location's boundary generation.
- Each call probably started one of the scans on REData's Overture database; not checked on REData's side.

The ordering this entry insisted on is met: REData's fix is in production, and UrbanLens 0.9.0, the first release that
reads Overture only from REData, has not shipped. PL9 plans the same order.

A point inside `is_usa_coordinates` but outside every shard REData syncs (Montreal, Nassau, Hermosillo, the western
Aleutians) gets "ok" with no rows from REData (its P97). UrbanLens does not ask there: `served_by_redata` also requires
one of REData's shard boxes, vendored as `boundaries.redata_overture_shards` and held to REData's by
`OvertureShardTableTests`, so those points read the public release. Still open on REData's side: a shard it has not
synced yet answers "ok" with no rows too, which UrbanLens takes as final. The handoff is
[`handoffs/redata-overture-near-point-lookups.md`](handoffs/redata-overture-near-point-lookups.md).

**Not verified:** the places lookup, `GET /points-of-interest/lookup/?provider=overture&radius_meters=150`, at the
Capitol on production, and how long the buildings answer took (the 2026-10-06 report gave a status and a count, not a
time). Close this when both answer in a few seconds with rows at the Capitol.

## P242 — Migration 0033's operator command can't run on the schema it is meant for, since 0040 added a Location column

`id: P242` · `status: open` · `updated: 2026-10-03` · `found by: the P241 test sweep`

Migration 0033 refuses to apply while any overlay still has only an `image_url`, and tells the operator to run
`download_overlay_image_urls` first, against the 0032 schema. Since P186's 0040 added `Location.official_name_source`,
that command fails with `UndefinedColumn` (`_store` does `select_related("parent_pin__location", …)`, and the image it
creates loads the owner's Location). The test that pins this path,
`test_download_overlay_image_urls.py::TheOperatorsPathTests::test_the_migration_proceeds_once_the_command_has_run`,
fails on `release/v_0_9_0`.

Who it affects: an install still below 0033 upgrading straight to 0.9.0. Production and staging ran 0033 with
v0.8.0. Any column a later release adds to a model on the command's path breaks it the same way, because the command
runs today's code against an old schema.

Options, for the release's migration squash (see the release migration notes): require upgrades to pass through
0.8.0 and drop the command and its test; or make the download part of an ordinary data migration's follow-up (a task
queued by 0033 itself) so no current code ever runs on the old schema. Not Jess's call unless the first option
changes the supported upgrade path.

**2026-10-04.** `test_the_migration_proceeds_once_the_command_has_run` is a strict xfail on `ProgrammingError`, so
the suite reports the gap and the test fails once it is fixed. Rewriting the command over the 0032 historical models
was considered and set aside: the download goes through `materialize_media_item` (quota, dedupe, storage), which a
copy over historical models would have to keep in step with. No document states an upgrade-path policy. The squashes
carry no `replaces`, but an install at production's v0.7 state can still migrate straight to 0.9.0, so requiring
v0.8.0 as a step would be a new rule. That makes the first option Jess's call (asked 2026-10-04).

## P277 — A first visit to a place still waits on every panel whose answer is not stored, so its tail is unchanged

`id: P277` · `status: open` · `updated: 2026-10-04` · `found by: Claude, implementing P53`

P53's fix leaves out the panels already known to be empty. That shortens a returning visit: on an ordinary place,
49 requests became 22 and the page settled at 2.4 s instead of 4.3 s. A panel with no stored answer still has to
load, fetch and poll, so the first visit to a place is as slow as before.

**Measured cold, 2026-10-04,** on `development_main` with every worker synced to the release branch (P53's caveat about
a stale panel worker no longer applies). A pin created through `/dashboard/map/add/` at Harlem Valley Psychiatric
Center (41.6597, -73.5650, no Location within 2 km), then opened at once with `p53-measure.mjs`: 169 requests, 119 of
them polls, a peak of 20 at once, settled at 39.1 s. The last panels to settle mostly settled as nothing (204):
Site Features at 39.1 s, the CRIS media at 37.5 s, the Property Records Overview at 37.5 s, Satellite at 32.6 s with
an image, Library of Congress at 29.3 s, GDELT at 25.1 s.

`ApiCallLog` for the same minutes shows what they waited on. REData's points-of-interest lookup and its CRIS
`cultural-resources/lookup/` each ran into UrbanLens's 30 s request timeout. Reference-document search answered 503
after 20.6 s, and the parcel lookup 503 three times, at up to 2.6 s each. News search took 14.4 s, and each imagery
composition about 5 s. So this tail is REData's latency on a point it has not seen, not the browser's lanes: six
lanes would start the same slow requests sooner but not finish them sooner. The points-of-interest timeout is P240's.

In a process that has not yet resolved a remote geo boundary, the probe also cannot decide CRIS's or Digital
Commonwealth's gate, so on the first page that process serves those load.

What is left of P53's options, both still undecided:

- **More lanes.** Six panel lanes instead of four cut the tail by about a third and raised the peak by two (P53's
  2026-09-06 measurement). The 2026-10-04 cold visit above was bound by upstream timeouts, which lanes do not shorten.
- **Have the answer stored before the first visit.** Background enrichment (`LocationCacheEnrichmentSource`)
  already warms some sources per location. Warming the rest when a pin is created would turn the first visit into a
  returning one, but only for a visit 30 s or more after creation, since the slow answers above take that long
  whoever asks. A bulk import would need to be left out, or it would spend REData's time on pins nobody opens.

## P279 — Legacy `BLOCKED` friendship rows may still record the wrong blocker

`id: P279` · `status: open` · `updated: 2026-10-04` · `split from P21, whose other half was fixed on 2026-10-04`

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

## P286 — A campus pin could lose its own National Register listing, because REData answered a point with the rows last found from it; fixed in REData v0.3.4, not yet seen on production

`id: P286` · `status: open` · `updated: 2026-10-06` · `follows: P228`

P228's link to NPS's record appears only when REData's `nps_nrhp` answer for the pin's point holds the listing. On
2026-10-04, HRSH's campus pin (location 97736) had none: REData's cached answer from that point was Isaac Roosevelt
House alone, while a `force_refresh=true` search from the same point found HRSH's own listing (89001166), its boundary
holding the pin.

REData keyed a resource to the last point that found it, and answered a point with the rows keyed to it
([handoff](handoffs/redata-cultural-resource-cache-keyed-by-last-search.md)). A search from a neighbouring point, such
as one of the campus's building pins, moved the shared listing away. The campus point kept a fresh but partial answer
for as long as any of its other rows stayed put. It also worked in reverse: a row the last live search did not find,
like Isaac Roosevelt House at about 540 m, kept answering. The same read served 59 of REData's providers.

UrbanLens's side is correct given a correct answer. After one forced refresh on development, the pin and its wiki
gained `National Register #89001166` and its National Archives record. The Property Records Overview named the
listing with its number, and Isaac Roosevelt House, whose boundary does not hold the pin, was not linked.

**Fixed upstream; the live check is pending.** REData's `ba890af5` (answer a point from what its own last search found)
and `e9f83ffb` are first tagged v0.3.4, in production since 2026-10-06 15:21Z, and on REData staging the register check
passed for all four primary campuses (the handoff's header). Not run against production.

Closes when HRSH's campus pin keeps its listing after a lookup from one of its building pins, against production.

---

## P304 — A Location History file over about 180 MB, or a GPX file over about 60 MB, fails its preview on the time limit

`id: P304` · `status: open, needs Jess` · `updated: 2026-10-05` · `split from P95`

A history file's visits and routes are what the confirmed import saves, so the preview keeps them to the end of the
file, and memory grows with it (1.26x RSS for Location History, 5.9x for GPX, at 16 MiB). What stops a large file now
is `PARSE_SOFT_TIME_LIMIT_SECONDS` (110 s), not memory. At the 2026-10-02 rates (16 MiB of Location History in 9.9 s,
16 MiB of GPX in 28.8 s, on a busy host), about 180 MB of Location History (about 230 MB RSS) or 60 MB of GPX (about
360 MB) is the largest that finishes. A larger one fails with "These files took too long to read. Try a smaller
upload."

**For Jess:** should such a file import? Options: a longer limit for history files; or writing visits and routes to
disk as they are read, which bounds memory but not time, so it only helps together with a longer limit. Leaving it as
it is refuses the rare multi-year export.

## P316 — Nine tests fail under `bin/host_pytest.sh` on `release/v_0_9_0`, from three causes

`id: P316` · `status: open` · `updated: 2026-10-05`

Measured on 2026-10-05 against `release/v_0_9_0` (`8ff61612f`) and `integ/merge2`, with the same nine failing on
both. Not run in a test-runner container (`bin/run_tests.sh`), so whether each is host-only is not known.

- `test_basemap_tile_cost` (3) and `test_basemap_tile_authorisation` (1) count statements, and under
  `host_pytest.sh` every request reads its session from the database (`expire_date > '<now>'`), which those
  budgets do not allow for. `test_slug_existence_side_channel` failed 443 cases the same way until its shapes
  ignored that timestamp.
- `test_import_parse_memory` (4) peaks near 8.4 MB for a 12 MB and a 16 MB archive alike, against budgets of
  1.5-2 MB. The peak does not grow with the file, so something fixed is allocated inside the measured window.
- `test_geolocation_ping_batches_boundaries::test_a_pin_with_no_boundary_counts_only_near_its_marker` finds a
  boundary polygon where its fixture expects none.

`test_write_route_smoke` and `test_websocket_credential_scopes` each failed once in a full run under load
and pass alone, on both branches.

## P318 — The import wizard sent each Google Maps CID through the browser as a JSON number, zeroing its low digits; fixed, but stored rounded CIDs in import failures need `fix_float_rounded_cids`

`id: P318` · `status: open` · `updated: 2026-10-05` · REData counterpart: REData `docs/PROBLEMS.md` P120 (and P116, the Google refusal it fed)

**What happened.** The import preview put each Takeout pin's CID into its JSON as a number
(`GoogleMapsGateway._iter_preview_pins`). The wizard's `res.json()` made it a JavaScript `Number`, and
the confirm step's `JSON.stringify` posted back that double's shortest digits padded with zeros:
`14522379626423718452` became `14522379626423718000`. A CID is an unsigned 64-bit integer, so every CID
above 2**53 - nearly all of them - lost its low digits. The confirm step trusted the posted value.

On 2026-07-31 the deferred lookup sent those as bare CIDs, and REData stored 1,945 of them on staging,
none of which ever resolved; its sweep asked Google about them every night, which spent the
`google_places` budget and helped get the LAN's address refused by Google (REData P120 has the
counts). `6a10bada7` (2026-07-31) started sending each pin's `maps_url` alongside, and REData derives
the CID from the URL's feature id, so REData stopped storing new rounded CIDs. The browser round trip
itself stayed: until this fix, a deferred pin was keyed by the rounded CID while REData answered under
the true one, so a Takeout pin above 2**53 that needed a lookup was never placed and ended as a
`LOOKUP_STALLED` `PinImportFailure` carrying the rounded CID.
`test_cid_float_rounding_import.py::TakeoutCidSurvivesTheBrowserTests` reproduces it against the old
code: the task schedules a retry instead of placing the pin.

**Fixed on `fix/cid-float-rounding`.** The preview sends `cid` as a decimal string
(`google/maps.py`, `_iter_preview_pins`). `services/apis/locations/cid_validation.py` holds every CID
check, with REData's rules and error codes. `maps.confirmed_pin_cid` checks a posted CID against its
own `maps_url`, uses the URL's CID when the posted one is its float-rounded copy, and skips the pin
otherwise. `cid_resolution.resolve_cids` refuses any CID that fails the checks before REData or
Google Places is asked. `RedataCidGateway` sends CIDs as strings and reads REData's per-entry
`rejected` list, which the deferred task records as a failure at once instead of retrying. The
place-details plugin treats a refused CID as no place. Property tests cover every 64-bit value through
the preview, the browser, the confirm step, the request body and the response
(`test_cid_validation.py`, `test_cid_float_rounding_import.py`, `test_redata_cid_gateway.py`), and
`import-wizard.test.ts` checks that a 20-digit CID string reaches the confirm body unchanged.

**Still open.** `python manage.py fix_float_rounded_cids` (dry run unless `--execute`) repairs a
float-shaped CID in `dashboard_pin_import_failures` from the failure's own `maps_url`, or deletes the
failure when nothing recovers it. It only counts float-shaped CIDs in `dashboard_google_places` and
keeps them: every row there holds coordinates, so its CID resolved, a rounded CID never resolved, and
a real CID has the shape about once in a few hundred. Its dry run on dev on 2026-10-05 found nothing:
dev was rebuilt on 2026-09-06 and none of its 2,678 Google place rows holds a CID. Production was not
read. A CID REData refuses is recorded with the existing
`LOOKUP_ERROR` reason rather than a new one, so its owner-facing wording ("lookup service was
unreachable") is imprecise for that case.


## P321 — A data export that includes photos or image overlays fails outright on object storage, which production 0.8.0 uses; fixed on `release/v_0_9_0`, live until 0.9.0 deploys

`id: P321` · `status: open` · `updated: 2026-10-06`

**What was wrong.** `_export_photos` asked every photo for `image.image.path`, and
`MapAnnotationsExport._overlay_row` asked every image overlay's file for `stored.path`, then copied from
that local path. `GatedS3Storage` (`services/media/object_storage.py`) is an `S3Storage`, and neither
defines `path()`, so Django's `Storage.path` raises `NotImplementedError`. Nothing in the export catches
it until `run_export`'s blanket `except Exception`. The whole export fails: no archive, the job status
reads "Export failed. Please try again.", and the log shows `Export failed for user <id>` with the
`NotImplementedError` traceback. The Celery task does not retry, because `run_export` returns False and
does not raise. Files are not silently dropped. That only happens on a backend whose `path()` returns a
path where nothing exists: `InMemoryStorage` does that, and the old code skipped the file there without
saying so.

An export fails when it includes `photos` (checked by default on the Tools page) and the account has any
`Image` row with a stored name, whether or not the object exists. It also fails when it includes
`map_annotations` and the account has any image overlay. An account with neither exports normally.

**Production impact: inferred from code and config, not observed.** On 2026-10-06 the infrastructure
repo's `origin/main` pinned `ghcr.io/urbanlens/urbanlens:sha-d1fb1bf`
(`platform/urbanlens-app/base/kustomization.yaml`). `d1fb1bf` is the 0.8.0 release commit, and its
`export.py` has both calls (lines 856 and 1214). The same repo's `base/deployment-web.yaml` and
`base/deployment-worker.yaml` set `UL_MEDIA_STORAGE_BACKEND=s3`, which selects `GatedS3Storage`. 0.8.0's
`uv.lock` has django-storages 1.14.6, whose `S3Storage.path is Storage.path`. Not checked: what the
cluster is actually running, the production logs, and whether anyone has requested an export there.

**Reproduced.** `test_export_on_object_storage.py` runs the real `run_export` over a path-less
`InMemoryStorage` subclass, and over `GatedS3Storage` built from production's `_S3_STORAGE_OPTIONS` and
answered by an in-process bucket (the `object_store` harness from `test_object_store_client_config.py`).
Against the old code, all five export tests fail on `run_export reported failure`, with the
`NotImplementedError` above.

**Fixed on `fix/export-on-object-storage`.** `export._copy_into_archive` opens the stored name through
its storage and streams it into the staging directory with `shutil.copyfileobj` in 1 MiB reads. It keeps
the `_{pk}` suffix when two files share a basename. A file storage does not have gives `filename: null`
and the row is still exported. That includes an object deleted between `S3Storage.open`'s existence
check and the download: the download fails with a 404 `ClientError`, which
`storage_errors.is_missing` recognises, and the partial copy is removed. Any other storage error still
fails the export, so the archive never has a hole that nothing reports. A test holds that for a 503 and
a 403. `_stored_modified_time` dates each archived copy as `shutil.copy2` did. On S3 it uses the
`Last-Modified` that `S3File` already loaded, so an object costs the export only the requests that
reading it does (two HEADs and a GET with django-storages 1.14.6). One change on the local backend: a photo whose file
was missing used to export its basename as `filename`, pointing at nothing (or at another photo's copy
with the same basename). It now exports `null`, like overlays always did. The importer counts both as
"missing from the archive".

**Memory.** `storage.open` + `copyfileobj` alone does not bound memory on S3. `S3File` downloads the whole
object on the first read into a `SpooledTemporaryFile` capped at `max_memory_size`, and `S3Storage`
defaults that to 0, which means it never spills, so every byte stays in RAM. With a 900 MB upload cap
(`SiteSettings.max_upload_file_size_mb`), one video could hold 900 MB in a worker with a 4 GiB limit.
`_S3_STORAGE_OPTIONS` now sets `max_memory_size` to 16 MiB, past which the download spools to a
temporary file. This covers every `S3File` read in the app, including video and document processing
(`videos.py`, `documents.py`), which copy the whole object out the same way.

**Other `.path` on a stored file in production code**, from
`git grep -nE '\.path\b' -- 'src/**/*.py' ':!*/tests/*' ':!*/migrations/*'` with each hit read. Only
`management/commands/anonymize_stored_photo_filenames.py:48-49` (`storage.path(old_name)`) remains, and it
already catches `NotImplementedError` and falls back to `storage.open`/`save`/`delete`. Thumbnails, EXIF
stripping, keywording, the malware scan, video and document conversion, and the comment-image scan all
read through `FieldFile.open("rb")`. The import side reads only its own extracted archive on local disk.
A related problem that is not a `.path` call, and is not fixed here:
`services/admin/media_usage.py:96` sizes `MEDIA_ROOT` with `os.walk`. On the S3 backend that counts
only the local scratch subtrees, so the admin panel's media size reads near zero in production.

**Still open until 0.9.0 is deployed.** Production keeps failing these exports until an image built from
`release/v_0_9_0` replaces `sha-d1fb1bf`. Close this entry when that happens.

## P322 — `src/bin/app.py` runs `main()` twice on direct execution and writes corrupt pins for versioned requirements

`id: P322` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard A (infra scripts), verified by re-reading the file.

Two independent bugs in the local bootstrap helper:

1. `src/bin/app.py:198-202` has two consecutive `if __name__ == "__main__":` blocks, each calling `main()`. Running the file directly executes the whole initializer twice (double builds, double `runserver` attempts). The second block looks like a copy-paste leftover; the first even carries the docstring `"""Run when called directly."""`.
2. `App.pip_install` accepts a versioned requirement (`src/bin/app.py:72` docstring example: `app.pip_install('requests==2.26.0')`), validates it via `Requirement(package_name)`, then writes `f"{package_name}>={version}\n"` (`src/bin/app.py:104`) — producing `requests==2.26.0>=2.26.0` in requirements.txt. The pin should use the parsed distribution name, not the raw input. A `TODO` at `src/bin/app.py:76` notes this method duplicates new djangofoundry functionality and should be removed on the 0.8 upgrade, which would moot both bugs.

No duplicate in `docs/PROBLEMS.md` or the archive (searched `app.py`, `pip_install`, `requirements`).

## P323 — `src/bin/db.py` hardcodes a `Z:` backup directory and `sanitize_path` keeps `/` and `.`, so traversal passes while legitimate paths are mangled

`id: P323` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard A, verified by re-reading the file.

1. `src/bin/db.py:232`: `BACKUP_DIR = "Z:/DEV/backups/db/postgres"` breaks every non-Windows run and ignores the `UL_*` env conventions used elsewhere in the same file.
2. `sanitize_path` (`src/bin/db.py:299-301`) strips characters with `re.sub(r"[^a-zA-Z0-9/_.-]", "", user_input_path)` — the allowlist keeps `/` and `.`, so `../../etc` passes through unscathed, while legitimate paths are silently mangled (`"My Documents"` becomes `"MyDocuments"`). It is applied to the `data_path`/`log_path` setters (`src/bin/db.py:58-83`) that feed `pg_ctl -D/-l`, so two distinct inputs can map to one path. Strip-don't-reject is the wrong shape: reject invalid paths instead of rewriting them.

No duplicate in `docs/PROBLEMS.md` or the archive (prior backup-controller notes in `docs/audits/codebase-audit.md:429-435` concern an older revision of a different controller).

## P324 — `src/bin/research.py` puts the API secret in the URL query string, parses blindly, and has no tests

`id: P324` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard A, verified by re-reading the file (`src/bin/research.py:1-30`, whole file).

`fetch_urbex_posts` interpolates the token into the URL (`requests.get(f"{INSTAGRAM_GRAPH_URL}search?access_token={INSTAGRAM_ACCESS_TOKEN}&q={hashtag}")`, line 8) — query strings land in logs, proxies, and error reports — instead of an `Authorization` header. The hashtag is uninterpolated/unencoded, the response gets no status check before `.json()`, and `main()` indexes `post["image_url"]` (line 23) with no `try/except`, so one malformed item aborts the run. `main()` also hardcodes `hashtag = "urbex"`. Logic-bearing code with zero unit tests anywhere in the shard.

No duplicate in `docs/PROBLEMS.md` or the archive.

## P325 — `CoreConfig` names the app `"core"` while every import says `urbanlens.core`, and `ready()` does I/O with no return annotation

`id: P325` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard A, verified by reading `src/urbanlens/core/apps.py:1-18` (whole file).

`name = "core"` resolves only if `src/` is on `sys.path` as a top-level `core` package, inconsistent with the `urbanlens.core.*` imports used everywhere else; app loading therefore depends on path setup rather than the package layout. `def ready(self):` has no return annotation (repo is MyPy-strict per `CLAUDE.md`), and its body constructs `DatabaseBackup()` — a constructor with an `auto_schedule` side effect that touches cache and enqueues work — plus `get_git_commit_at_start()` at app-registry time.

No duplicate in `docs/PROBLEMS.md` or the archive.

## P326 — `FriendInvitation.save()` and `TriviaQuestion.save()` ignore `update_fields`, so scoped saves go stale or write full rows

`id: P326` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard B (models), verified by re-reading both methods. P5 class; these instances are new.

1. `src/urbanlens/dashboard/models/friendship/invitation/model.py:66-72`: `save()` recomputes `email_normalized` then calls bare `super().save(*args, **kwargs)`. A caller passing `update_fields=["email", ...]` writes `email` without its normalized companion (match/dedup column goes stale); a caller excluding `email` still pays a full-row write. The correct pattern already exists in-repo at `src/urbanlens/dashboard/models/safety/model.py:452` (`if update_fields is not None and "email" in update_fields: kwargs["update_fields"] = {*update_fields, "email_normalized"}`).
2. `src/urbanlens/dashboard/models/trivia/model.py:124-127`: `save()` unconditionally rewrites `answer_normalized` and calls bare `super().save()`, unlike `Pin.save`, `Location.save`, `AliasBase.save` which inspect `update_fields`. Any scoped caller touching an unrelated column rewrites both answer columns — the overwrite/`updated`-bump/noise shape P5 describes.

P5 ("Dialog forms still post every field…", open) is the umbrella; these are two concrete model-layer instances not listed in its table. Cite both when touching either file — `see P5 and P326 in docs/PROBLEMS.md`.

## P327 — `EpaFacility.record_search/detail_result` merges `data` with unlocked read-modify-write, so concurrent enrichments clobber each other's keys

`id: P327` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard B, verified by reading `src/urbanlens/dashboard/models/epa_facility/model.py:80-125`.

Both classmethods do `entry.data = {**entry.data, **data}` then `entry.save(update_fields=[...])` (lines 87-88, 118-120). `_get_or_create_row` hardens only the create race (line 135 `except IntegrityError`); the merge path has no `select_for_update`/retry, so two concurrent enrichments each merge against a stale dict and the second write silently drops the first's keys. Background-task concurrency makes this reachable, not theoretical.

No `EpaFacility` hits in `docs/PROBLEMS.md` or the archive — appears new.

## P328 — `SearchHistory` uniqueness is case-sensitive with no normalized column, so `Paris` and `paris` are two rows

`id: P328` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard B, verified by reading `src/urbanlens/dashboard/models/search_history/model.py:15-45`.

The constraint is `UniqueConstraint(fields=["profile", "query"], name="uniq_search_history_profile_query")` (line 39) on the raw `query` CharField (line 19). No `query_normalized` column or `Lower()` constraint, unlike `PinAlias` (`aliases/model.py:103`, `UniqueConstraint(Lower("name"), "pin", ...)`) and the `FriendInvitation.email_normalized` pattern. Case variants of one query accumulate as separate rows with separate `use_count` counters.

No duplicate in `docs/PROBLEMS.md` or the archive (archive hits concern history *scoping*, not the dedup key).

## P329 — `assistant/message` and the search hints/commit/history-delete endpoints have no throttle while their sibling routes do

`id: P329` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard D (controllers), verified in `src/urbanlens/dashboard/urls.py:2030-2045` and `src/urbanlens/dashboard/controllers/assistant.py:220-234`.

`search.panel` is wrapped in `throttled("search.panel", ...)` but the adjacent `hints/`, `commit/`, and `history/delete/` routes are bare. `_verified_hints` (`controllers/search.py:61-73`) runs a full `engine.search` per candidate until 4 verify — up to ~8 full ~11-provider fan-outs per request — so one hints request costs a multiple of one panel request, cached 15 min per profile. `assistant/message/` (`urls.py:177`) is likewise bare: `AssistantMessageView.post` does `get_or_create`, history load/save, and render on every POST. The single-flight turn lock bounds *AI spend* (a second POST while a turn is in flight is dropped with a toast), but nothing bounds request/DB/render volume over time on an chat surface.

Related to the archived G5-3 throttled-routes list and H42/H49 (panel throttle + query cap), which never mention these endpoints — sibling-gap of a known fix, not a duplicate.

## P330 — `RemoteImageCopyView` answers anonymously and `/metrics` serves with neither gate configured — both weaker than their siblings

`id: P330` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard D, verified by reading the cited code. Two endpoints, one entry: each skips the gate its sibling paths enforce.

1. `controllers/remote_copies.py:36-77`: `RemoteImageCopyView` is a plain `View` with no `LoginRequiredMixin`; route `media-copy/<str:digest>/` (`urls.py:400-403`) has no auth gate. Every sibling media path is default-deny (see P14). Mitigations that keep this low-medium rather than high: the digest is unguessable-ish, and misses are throttled (`COPY_THROTTLE_SCOPE`) — but a miss still lets an anonymous caller queue worker fetch work.
2. `services/core/metrics_auth.py:24-26,43-45`: `token_ok` and `network_ok` each `return True` when unconfigured ("the gate is off"), the route registers on `metrics_enabled` alone (`UrbanLens/urls.py:127-131`), and `gates_configured()` is consumed only by a management-command warning (`management/commands/celery_metrics_exporter.py:61`), never by the view (`controllers/metrics.py:91-108`). Enabling metrics without either gate serves operational telemetry — including per-worker internals — to anyone.

No `PROBLEMS.md`/archive entry mentions either endpoint's auth — appears new.

## P331 — The OAuth authorize/introspect views have zero test references anywhere in the repo

`id: P331` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard E (coverage mapping), verified: `rg -l "ConsentAuthorizationView|ActiveOwnerIntrospectTokenView" --glob '*.py' .` returns only `src/urbanlens/UrbanLens/urls.py` (wiring `oauth/authorize/`, `oauth/introspect/`) and the two controller files themselves (`controllers/oauth_authorize.py`, `controllers/oauth_introspect.py`). No test file — `dashboard/tests/`, `tests/`, contract or integration — imports or names them. Token-introspection logic that disagrees with its validator is security-adjacent; it currently ships with no regression pin at all. (Caveats checked: `child_buildings` and `e2ee_schema` also miss by name-string but are covered behaviorally via `test_child_building_details.py` and `test_external_api_schema_e2ee.py`; the two OAuth modules have neither.)

No entry in `docs/PROBLEMS.md`, the archive, or the PL6 test-quality-audit manifest names these controllers — appears new.

## P332 — Pin CSV exports write unsanitized user content, enabling spreadsheet formula injection

`id: P332` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard C (services), verified by reading both writers.

`services/import_export/export_formats.py:88-91` (`writer.writerow([pin.effective_name, ... pin.description or ""])`) and `services/import_export/export.py:708-716` write pin names/descriptions verbatim via `csv.writer`. A value beginning with `=`, `+`, `-`, `@` (e.g. `=HYPERLINK("https://evil/","label")`) survives CSV quoting and is evaluated by Excel/LibreOffice after parse. Names and descriptions arrive from user input and file imports, so attacker-influenced text reaches a victim's export (import a crafted file → export → open). KML/GPX/GeoJSON writers are unaffected (structured libraries/`json.dumps`). CWE-1236, low severity (requires the victim to open the export in a formula-evaluating app).

Nearest neighbor is open P321 (same export feature, server-side `path()` crash on object storage — a different failure mode). Archive CSV entries are all import-side. Appears new.

## P333 — `VersionedModel` provenance recording swallows all exceptions in production, leaving write-succeeded/provenance-missing gaps silent

`id: P333` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard B, verified by reading `src/urbanlens/dashboard/models/abstract/versioned.py:240-254`.

The revision `bulk_create` (in its own savepoint, with a comment explaining the aborted-transaction hazard) is wrapped in `except Exception: logger.exception(...)` with re-raise only `if django_settings.DEBUG or TESTING`. A failing revision write therefore leaves the data write succeeded but its field-provenance rows missing, visible only in logs — the exact substrate `resolve_fields` callers cannot detect. The "never break the write" tradeoff is deliberate; the silent-integrity gap is still worth recording before offline access and merging land (same reasoning as P5's provenance paragraph).

No `PROBLEMS.md`/archive entry for `_record_fields` swallowing — appears new.

## P334 — `InferenceRequest.max_tokens` has no lower bound, so `0`/negative passes schema and policy and fails later as a provider error

`id: P334` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard A follow-up, verified by reading the cited code.

`src/urbanlens_ai/schema.py:107` declares bare `max_tokens: int` while the sibling field carries `timeout_seconds: float | None = Field(default=None, gt=0)` (`schema.py:110`). `policy.validate_request` (`policy.py:207-208`) only checks the ceiling (`if request.max_tokens > MAX_ALLOWED_TOKENS`), so `0` or a negative value passes both layers and fails downstream as a provider rejection instead of a 400. Fail-closed, wasted call at worst — low severity, one-line fix (`ge=1`).

No duplicate in `docs/PROBLEMS.md` or the archive (only `docs/AI_PIPELINE.md:53` on the over-cap upper bound).

## P335 — Test harness gaps: the network guard does not patch DNS, and the throwaway TLS key is world-readable

`id: P335` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard A follow-up, verified by reading the cited code. Two test-support gaps, one entry — both are the harness being looser than its documentation.

1. `src/urbanlens/core/testing_network.py:105-109` patches exactly `socket.create_connection`, `socket.connect`, `connect_ex`, and `sendto`. Nothing intercepts `socket.getaddrinfo`/`gethostbyname`, so a test doing name resolution still emits real DNS traffic despite the module docstring (`testing_network.py:1`: "Fail fast on accidental external network calls in tests"). The archive records only the `connect_ex` gap as fixed (`PROBLEMS-ARCHIVE.md:17953-17958`); DNS is unmentioned.
2. `src/urbanlens/core/tests/slow_servers.py:72-82` writes the throwaway private key with bare `open(key_path, "wb")`, inheriting the umask (typically 0644) with no `chmod(0o600)` anywhere. Mitigations that keep this low: the `mkdtemp` dir is 0700, the key is ephemeral EC, the cert lives one hour, test-only loopback.

No duplicate in `docs/PROBLEMS.md` or the archive for either half.

## P336 — Three small `bin/` helper defects: dead CodeQL arm64 branch, `map_layers.py` wrong usage string and untyped `main`, settings-parser typo and placeholder divergence

`id: P336` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard A follow-up, verified by reading each file. Grouped as one entry: all trivial, all in operator/dev helpers.

1. `bin/run_codeql.py:181-183`: `if machine in {"arm64", "aarch64"}: return "codeql-bundle-osx64.tar.gz"` followed by an identical default return — Apple Silicon silently fetches the Intel bundle (works via Rosetta, slower) with no comment.
2. `bin/map_layers.py:7-9`: usage string says `python convert_to_webp.py ...` though the file is `map_layers.py` and no `convert_to_webp.py` exists; `def main():` (`map_layers.py:59`) is untyped while its helpers are annotated; `unique_path`/`convert_image` have no tests (repo-wide grep hits only the file itself plus `unused_functions.txt:821-822`).
3. `src/bin/utils/settings.py:61`: `raise FileEmptyError(f"No data in settings file at f{filepath}")` renders `at f/...` (error path only, cosmetic); `:55` uses `yaml.load(file, Loader=SafeLoader)` instead of `safe_load` (functionally safe, non-idiomatic); `:20-22` define `INSTAGRAM_GRAPH_URL`/`GOOGLE_LENS_URL` as `"your-*-placeholder"` strings while `src/bin/settings.py:3-7` holds the real defaults — two sources of truth for the same keys.

No `PROBLEMS.md`/archive entry for any of the three (the `P324` research-helper entry is the same shard but a different issue).

## P337 — `unique_together` is still used in ~9 model files instead of `UniqueConstraint`

`id: P337` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard B follow-up; coordinator confirmed `rg -l unique_together src/urbanlens/dashboard/models/` hits `friendship/model.py`, `location/model.py`, `location/queryset.py`, `safety/model.py` (`SafetyCheckinPartner`, line 584), `cache/location_cache.py`, `reviews/model.py`, `abstract/model.py`, `trips/model.py`, `trips/queryset.py`.

Django still enforces it (soft-deprecated in favour of `UniqueConstraint`, no removal scheduled), so this is style/forward-compat debt, not a live bug — info/low severity. Filed so the tree-wide shape is recorded once rather than rediscovered per model; fix is mechanical per file.

Archive mentions of `unique_together` are incidental (friendship pairs, lat/long races); no open entry asks for the migration.

## P338 — Device-scan marker path has no composite indexes and reconciles clusters without a transaction

`id: P338` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shards B/C follow-up, verified by reading the cited code. Same subsystem (device_scan), one entry.

1. `DeviceScanEntry.Meta` (`models/device_scan/model.py:180-181`) and `DeviceSignalReading.Meta` (`:202-203`) declare only `db_table` — no `indexes`/`constraints`. Django's default per-FK B-tree covers single-column lookups; what is missing is composite/query-shaped indexes (e.g. `(entry, observed_at)`) for the clustering reads. Sibling models show the pattern (`ScannedDevice.Meta:102` `idxdb_scandev_mac`, `DeviceScanUpload.Meta:142-148`). Low severity: perf only, clustering reads are bounded batch jobs.
2. `services/device_scan/clustering.py:254-267` loops clusters with per-cluster field assignment and bare `marker.save()` (full-row, N round trips; the STALE sweep 20 lines below at `:283-286` uses `update_fields`), with no `transaction.atomic` in the function or its caller (`pipeline.py:11-42`; contrast sibling `ingestion.py:66`). A crash/retry mid-loop leaves a half-updated marker set — self-healing on the next recompute (the function rebuilds from scratch, `:214-222`), so crash-consistency only. Fix: one `atomic()` around the reconcile plus `update_fields` on the hot path.

`DATA_ENCRYPTION.md` Follow-up #9 covers device-scan retention/encryption, not indexes; no `PROBLEMS.md` entry on either half. (`DATA_ENCRYPTION.md` Follow-up #9 and the Jess 2026-09-29 never-delete decision for `DeviceScanUpload.profile = SET_NULL` are deliberate and not challenged here.)

## P339 — 66 nullable string columns create NULL-vs-`""` ambiguity tree-wide; only 5 are tracked

`id: P339` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard B follow-up; coordinator counted 51 `CharField…null=True` lines across 17 files plus `TextField…null=True` in 11 files under `src/urbanlens/dashboard/models/` (66 sites, consistent with the shard's "~60"). Representatives: `images/model.py:260` (`caption`), `location/model.py:43` (`official_name` + 7 address siblings), `pin/model.py` (5×), `wiki/model.py` (4× + 1× TextField), `profile/model.py` (2× TextField).

Django convention is `blank=True, default=""` for strings; dual null/empty states split `__isnull` vs `=""` queries and `Lower()`-unique semantics. Individually trivial, collectively query-correctness drag — low-medium. The fix direction is already documented (`DATA_ENCRYPTION.md:245-250` prescribes `blank=True, default=""` for new `fail_soft` content fields; Follow-up #4 tracks converting the five encrypted ones with a demonstrated data-loss consequence). This entry records the tree-wide count so the remaining ~60 are not each rediscovered.

No `PROBLEMS.md` entry quantifies the pattern as such — appears new.

## P340 — AI gateway and global-search error logs capture full prompt queues, full model responses, and verbatim queries

`id: P340` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard C follow-up, verified end-to-end (log call → what the logged object contains → absence of redaction).

- `services/ai/gateway.py:372`: `logger.error("No answer from message queue: %s", queue)` where `queue` is a `MessageQueue` whose `__str__` is the full message list (`services/ai/message.py:73-74`), built from system + user prompts (`gateway.py:321,334`).
- `services/ai/gateway.py:454`: `logger.error('No ANSWER in response from AI model "%s": Response: %s', self.model, message_content)` where `message_content` is the full model response text (`:366-370`).
- `services/global_search/engine.py:163`: `logger.exception("Global search provider '%s' failed for query %r", provider.slug, parsed.raw)` where `parsed.raw` is documented as the query exactly as typed (`services/global_search/parser.py:90,497`).

The prompts are not just typed text: callers wrap untrusted content via `wrap_user_data` (`document_import.py:321`, `article_expansion.py:230-247`, `link_extraction.py:569`, `article_safety.py:90`). A grep of `gateway.py`'s logger calls shows only token counts/model names — no `redact_*`, no truncation (the only AI redaction in-tree is URL redaction in `link_extraction.py`, not applied here). Error-path only, but server logs then hold full prompts (PII, pin names, article text, document contents) and full answers — medium severity, log hygiene.

Not the archive's coordinates-in-logs entry (that grep matches log calls naming latitude/longitude/coords — `queue`/`message_content`/`parsed.raw` contain no such token) and not P212's Celery `args/kwargs` redaction (different sink). Appears new.

## P341 — Held-upload publish reads the whole file into memory with no bound; icon uploads can be 250 MB

`id: P341` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard C follow-up, verified including the size-ceiling asymmetry.

`services/media/held_upload.py:229-232` does `raw = handle.read()` then re-encodes from `BytesIO(raw)` — no size check, no chunked read. The function's contract (`held_upload.py:141`: "already size-checked, sniffed and scanned") pushes the ceiling onto callers, and callers differ: avatar uploads enforce `AVATAR_MAX_UPLOAD_BYTES` (default 5 MB, `services/profile/avatar.py:314-316`, pinned by `test_avatar_upload_is_bounded.py`), while icon doors (label/pin/achievement `custom_icon` via `HELD_FIELDS`, `held_upload.py:104-112`, capped only by `ICON_MAX_PX = 256` *dimensions*) call only the shared `image_upload_error` gauntlet whose sole size gate is the site-wide cap (default 250 MB, raisable toward 900 MB). So a 250 MB icon passes validation, is held, then fully materialised in RAM on publish. Bounds on severity: the read runs in the sandbox worker, not gunicorn, and icons still pass the synchronous malware scan at request time — medium, memory/DoS amplification in the memory-tight sandbox worker (`MEDIA_PIPELINE.md` §2.4), re-queueable via the retry/sweep path (`:337-381`).

No `PROBLEMS.md` entry; archive held-upload mentions are publish-mark races and orphan lifecycle, not the read. Appears new.

## P342 — Safety-contact mark-safe/opt-out token POSTs have no throttle; the message route already has a service-layer budget

`id: P342` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard D follow-up, verified view by view.

`SafetyContactMarkSafeView.post` (`controllers/safety.py:1633-1645`) and `SafetyContactOptOutView.post` (`:1686-1700`) mutate check-in state behind an anonymous UUID capability with no decorator, counter, or rate check, and no `throttled()` wrapper on any contact route (`urls.py:1889-1892`) — contrast `e2ee.login_params` throttled per address (`urls.py:2010-2012`, `Rate(30, 60)`). The third contact route was checked and is *not* part of this problem: `SafetyCheckinMessageView.post` is budgeted at the service layer (`charge_message` per-participant-per-checkin, `MessageRateLimitedError` → 429). Severity low: token is a UUID path segment (404 otherwise, not enumerable) and both writes are idempotent (second mark-safe short-circuits, `:1705-1715`), so the exposure is unauthenticated DB-write + notification fan-out replay, not takeover. The message route is the in-repo pattern to copy.

Open P329 covers throttle gaps on assistant/search routes, never safety contacts; the archived G5-3 throttle list never mentions them. Appears new.

## P343 — WebSocket bearer credentials travel only as `?key=`, which persists in logs, history, and referers

`id: P343` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard D follow-up; coordinator confirmed `_extract_token` (`websocket_auth.py:63-68`) parses only the `key` query-string parameter — the sole credential parse in the file, with no header/cookie/subprotocol branch — and it is consulted for anonymous sessions (`:54-60`, class docstring `:35`).

A PAT or OAuth2 token therefore travels only in the URL; browsers keep session-cookie auth via the outer stack, but non-browser clients have no alternative transport. Query strings persist in access logs, history, referers, and error text (P203's `SecretRedactionFilter` mitigates app-log leakage but does not bless credential-in-URL). Scope checks themselves are correctly placed pre-`group_add` with 60 s revalidation — transport only, not scope. Medium-low severity, standard hygiene.

Archive websocket-auth entries cover PBKDF2-off-thread and scope enforcement, never the transport. Appears new.

## P344 — Health probes opt out of throttling while doing per-call cache writes and DB reads

`id: P344` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard D follow-up, verified with mitigations.

`controllers/health.py:64-66` sets `authentication_classes = []`, `permission_classes = [AllowAny]`, `throttle_classes = []`, and the routes carry no `throttled()` wrapper (`UrbanLens/urls.py:110-114`). The endpoints do real work per call: a cache **write** on every call (`:214`), DB probes (`:185-205`), and a migration-graph inspection (`:223-239`). Mitigations that keep this low: 30 s/5 s process memos on the expensive halves (`:39-40`, consumed `:126-127`) and a 2 s `statement_timeout` scoped to the probe transaction (`:30,43-54`) — so per-scrape cost is ~1 cache write + 1–2 cheap statements, not a full graph build. Residual is cache-write-per-scrape + DB-read amplification by an anonymous poller against intentionally-unauthenticated LB/K8s probes.

No `PROBLEMS.md`/archive entry on health throttling. Appears new.

## P345 — Two more P5 instances: onboarding does a whole-row save, profile autosave rewrites every field

`id: P345` · `status: open` · `updated: 2026-10-06`

Found by spark-audit shard D follow-up, verified; neither instance is in P5's table (`PROBLEMS.md:52-136`; `rg -i onboarding|_save_profile|ProfileForm|WelcomeOnboarding` over `PROBLEMS.md` = zero hits).

1. `forms/onboarding_form.py:72-97`: `Meta.fields = []`, then `save()` sets ~8 columns (`track_pin_visits/track_routes/track_geolocation/community_enabled/external_apis_enabled/places_*/ai_enabled/tos_accepted_at`) and calls bare `instance.save()` — whole-row write. Once-per-user, low concurrency risk, but exactly the last-writer-wins shape.
2. `controllers/userprofile.py:683-685`: `form.save(commit=False).save(update_fields=list(form.fields))` — scoped to the form's ~dozen columns (`forms/profile_form.py:58-68`) yet rewritten in full on **every autosave** (frontend is per-field `data-autosave`, server rewrites all): concurrent-tab revert risk, `updated` bump, and per-P5 ~14-write provenance noise per save.

P5 (open) is the umbrella and P326 added two model-layer instances; these are the form/controller counterparts. Cite all three when touching the area — `see P5, P326 and P345 in docs/PROBLEMS.md`.

## P346 — The assistant turn concatenates history, user message, and tool results with no user-data delimiters; tool-arg validation is thin and confirm re-executes under a stripped context

`id: P346` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (AI), verified by reading each site. One entry, three trust-boundary gaps in the same assistant loop.

1. `services/ai/assistant.py:84-90` serializes history as bare `ROLE: content` lines, `:126-127` appends `USER: {user_message}` unwrapped, and `:184` appends tool calls/results as unwrapped JSON — while every other AI feature wraps untrusted content in `wrap_user_data` (`link_extraction.py:569`, `article_expansion.py:230,254`, `article_safety.py:90`, `document_import.py:321`). The registry wraps only declared `user_content_fields` per tool result (`tools/registry.py:189-203`); history text, the live message, and the envelope itself are never wrapped. `send_with_tools` scans the blob but scanning is not a boundary — the system prompt's `<USER_DATA>` contract (`meta.py:8-14`) has nothing to key on. Medium-high: prompt injection shapes the tool calls the user confirms.
2. `tools/registry.py:42` URL gate is `re.compile(r"https?://")` — misses `data:`, `javascript:`, protocol-relative, encoded, and bare-domain exfil shapes (`:267-268`). `tools/trips.py:58-60` `CreateTripArgs.name` defaults to `""` with no `min_length` (compare `places.py:101-102` `min_length=1`), so the model can propose — and after one click create — an empty-named trip. Low-medium.
3. `controllers/assistant.py:351-352` re-executes cached AI-chosen args with `ToolContext(profile=profile, now=timezone.now())`, dropping the turn's `page`, `deadline`, and `dismissals` (`assistant.py:129`). `registry._is_available` (`:233-240`) re-evaluates `needs_page`/`requires_external_apis`/deadline against different facts than the turn saw — a TOCTOU window inside the 15-min proposal TTL (`turns.py:29`). Latent today (no tool sets `needs_page=True`); low.

P340 covers logging the prompt, P329 the route throttles — neither covers delimiting, arg validation, or confirm context. Appears new.

## P347 — Every AI feature inherits the same 16,000-token output budget and, except link extraction, no per-user daily cap

`id: P347` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (AI), verified by reading the gateway and all callers.

`services/ai/gateway.py:46` defaults `max_tokens = MAX_TOKENS` (`meta.py:2`, 16000), wired to the wire at `:402` as the only `max_tokens` any feature sends. No caller narrows it (`link_extraction.py:659`, `article_expansion.py:213-217`, `article_safety.py:80`, `document_import.py:238` pass no budget arg; only vision bypasses with `_KEYWORD_MAX_TOKENS = 300`). A keyword/category call can offer 16k completion tokens. Separately, only link extraction has a per-user daily cap (`extractions_remaining_today`, enforced at `link_extraction.py:460` against `SiteSettings.ai_link_extraction_daily_limit`); assistant (turn-bounded by `MAX_ROUNDS = 4`/`MAX_TOOL_CALLS = 6` but not turns/day), article expansion/safety, document import, and vision have none — the cost ledger (`call_log.py`) prices but never refuses. Both medium (spend control, not correctness).

Adjacent to P334 (the schema/policy *lower* bound) and P329 (request throttles bound concurrency, not spend) — neither names call-site budgets or daily caps. Appears new.

## P348 — The concealed wiki payload leaks exact latitude/longitude to viewers it exists to withhold detail from

`id: P348` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (geo/wiki), verified by reading the payload builder.

`services/wiki/wiki_detail.py:133-134` reads `wiki.latitude`/`wiki.longitude` and emits them verbatim at `:161-162` (`float(latitude)`), alongside `_boundary_geojson` (`:36-37,165`). The conceal branch swaps only `shown` fields, `cover_photo`, and row sets — coordinates pass through, and `ALWAYS_UNSET` (`wiki/concealment.py:28`) lists only security indicators, never coordinates. Per `PRIVACY_MODEL.md` §§1-3 exact location is the gated asset. Latent while `concealment_active` returns `False` (`concealment.py:112-121`), live the day it flips — medium, and cheaper to fix before the flip than after.

The *boundary-geometry* half is already recorded (`designs/concealment-review-2-2026-08-24.md:155` names `wiki_detail.py:171`); the exact lat/lng float half has no entry. Filed as its own problem so the geometry fix does not silently close it.

## P349 — Plugins run unsandboxed in-process: entry points and settings modules are arbitrary code with full DB and keys

`id: P349` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (tasks/plugins), verified by reading the loader.

`plugins/registry.py` `_load_entry_points` (`ep.load()`) and `_load_settings_modules` (`importlib.import_module(module_path)`, every `UL_PLUGIN_MODULES` path) register code with no import sandbox, no permission model, no timeout; `discover()` runs in `AppConfig.ready()`. Contribution aggregation and the hook bus (`hooks.py:97-101`) contain only *failures* (log-and-skip) — not *effects*: a plugin that imports fine already ran module-level code with settings, ORM, `UL_FIELD_ENCRYPTION_KEY`, provider/OAuth keys, and filesystem in scope before any `try` applies. `is_enabled()` gates contribution *use*, not import. High integrity severity by design (whole-app blast radius for any malicious/buggy plugin); no privilege boundary exists, so "fix" means defining one (signing, subprocess isolation, or an explicit trusted-code policy).

No sandboxing/blast-radius entry in `docs/PROBLEMS.md` or the archive (hits are the mypy plugin and builtin-provider refs). Appears new.

## P350 — Video location strip clears only container-level tags; stream-level location tags survive remux and re-encode

`id: P350` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (media), verified by reading the ffmpeg arg builders.

`_LOCATION_TAGS` (`services/media/videos.py:76-80`) feeds `_clear_location_args` (`:136-141`), which emits only global `-metadata {tag}=` args. Both consumers use those args alone: `_reencode` (`:162-183`) and `_remux_without_location` (`:187-189`, `-c copy` — preserving all stream metadata verbatim). No `-metadata:s:v`/`-metadata:s:a` variant exists, and `extract_video_metadata` keys `needs_strip` off global `fmt_tags` only (`:110-125`), so a stream-tagged location is neither detected nor stripped while the feature advertises a scrub. Medium (location survives an explicit "scrub"; photo path has an analogous `has_gps` exemption at `:232-236`).

`PROBLEMS.md`/archive grep for remux/`_LOCATION_TAGS`/stream tags hits only container-tag handling and `test_video_location_strip.py` (container-level). Appears new.

## P351 — Import pipeline ceilings: every archive JSON is `json.load`ed unbounded, and the extraction budget falls back to 64 GiB

`id: P351` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (media), verified by reading both functions. One entry, two ceilings in the same import path.

1. `_read_json` (`services/import_export/import_data.py:681-687`) is bare `json.load(fh)`, used for `manifest.json` and every `metadata.json`/`trips.json` (`:1563`, `:1675`) — while `_scan_extracted_files` explicitly *skips* `.json`/`.csv` in the malware+mismatch scan (`:640-643`) and the shard's own `json_stream.py:3-4` warns `json.loads` "holds Python objects several times the file's size". The archive byte ceiling bounds disk, not RSS multiplication. Medium (sandbox-contained, but OOM/kill + retry loop on attacker-shaped archives).
2. `_extraction_size_ceiling` (`:488-502`) returns `_EXTRACTED_BYTES_FLOOR * 32` = 64 GiB when the quota is unknown (`profile=None` callers, quota-resolution failure), used as the live per-byte budget in `_extract_zip_members_bounded` (`:571-619`). Low-medium (production passes a profile; the write-then-check loop still writes up to ceiling before refusing).

P304 covers preview-time limits, not `_read_json`; archive `:20677` notes `_read_json` "fails the task" with no memory bound. Appears new.

## P352 — Four more upstream-bound tasks sit on INTERACTIVE outside P167's list

`id: P352` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (tasks), verified against the decorators and P167's roster (`docs/PROBLEMS.md:2064-2075`).

- `fetch_panel_source` (`tasks.py:3813-3814`): live third-party panel fetch, `soft_time_limit=110, time_limit=130`, `queue=Queue.INTERACTIVE` — a 130 s upstream call on the 4-slot prefork worker alongside safety sweeps and signup mail. Deliberately no autoretry, so queue placement is the whole story. Medium-high (same starvation shape as P167, new occupant).
- `score_reputation_event` (`tasks.py:4846-4847`, INTERACTIVE; docstring: photo input "can mean walking external gallery panels - by far the most expensive input"), `refresh_pin_web_search` (`tasks.py:3463-3464`, INTERACTIVE, live `search_web` per missing search), `prewarm_spotguessr_round/solo_start` (`tasks.py:4442+`, INTERACTIVE, live Street View warm via `GoogleMapsGateway`). All network-bound, user-independent, `autoretry OSError 3x`. Medium.

None appears in P167/P113/P125. Same fix direction as P167 (move off INTERACTIVE); cite both — `see P167 and P352 in docs/PROBLEMS.md`.

## P353 — The upstream breaker and slot guard fail open on cache outage — protection drops fleet-wide exactly when the upstream is hot

`id: P353` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (core), verified by reading both guards. One entry, two halves of one failure mode.

- `UpstreamBreaker.wait` (`services/core/upstream_breaker.py:84-91`): `except _CACHE_ERRORS: return None` — "an unreadable cache lets the call go out." A Dragonfly outage turns every breaker-guarded call into pass-through, hammering an already-throttling upstream. Contrast `call_tally`/`counters` REFUSE policy for the same someone-else's-budget limits.
- `KeyedUpstreamSlots.hold` (`services/core/upstream_slots.py:131-133,151-153`): `except _CACHE_ERRORS: yield True` — per-process bound holds, fleet-wide per-account bound gone, so one account holds its cap in every process simultaneously. Deliberate per docstring, but a fail-open of an anti-abuse bound.

Low-medium (cost/429 amplification during outages). P157 names counters/locks/socket-budget policies, not these two; archive breaker entries cover scope/429 handling, not outage behavior. Appears new.

## P354 — Three check-then-act races: breaker `trip()` can shorten a recorded wait, outbox drains double-enqueue, WebAuthn cap/dup checks race

`id: P354` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (core/auth), verified by reading each site. One entry, one bug class in three places.

1. `upstream_breaker.trip` (`:126-133`) get-then-sets despite its "never shortening a longer wait" docstring: two concurrent `trip()` calls interleave get/get/set/set and the shorter wait wins. Fix is a Lua compare-and-hold (like `counters.delete_if_value`). Breaker timing only — low.
2. `drain_outbox` (`services/core/task_outbox.py:94-122`) reads `due()` rows, `apply_async`s, then `delete()`s with no select-for-update/claim column — two beat replicas (or overlapping runs) enqueue the same row twice. Safe only if every outboxed task is idempotent, which the module never states. Low.
3. WebAuthn registration checks the 10-credential cap and credential-id uniqueness before create (`services/auth/webauthn.py:148-150,225-243`) while `credential_id` is `unique=True` (`models/account/model.py:56`): a concurrent double-submit turns `CredentialAlreadyRegisteredError` into a raw 500 `IntegrityError`, and two parallel ceremonies push an account to 11. Low.

P157 covers counters/locks races, not these three. Appears new.

## P355 — The verification-email resend path never releases its inflight reservation, letting a user self-exhaust their email budget

`id: P355` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (core/security), verified caller by caller.

`email_rate_limit_error` (`services/security/email_safety.py:89-97`) takes a cache reservation (`cache.add` + `incr`, 300 s TTL) counted against the hourly/daily/monthly limits; the caller must return it via `release_email_reservation` (`:113-120`). Both in-scope owners pair them (`services/auth/email_claims.py:154-155`, `services/social/friendship.py:680-681`). But `controllers/userprofile.py:808-814` calls the limiter then, on success, only `record_email_sent(...)` + `queue_confirmation(...)` — no release. Each resend inflates `logs.count() + inflight` until TTL expiry. Low (self-inflicted budget exhaustion, 5 min per resend).

`PROBLEMS.md` greps for the reservation show only P147 enumeration entries. Appears new.

## P356 — Image/byte downloads buffer uncapped `response.content` in memory across four gateways

`id: P356` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (apis), verified per call site.

`apis/flickr/gateway.py:209-216` and `flickr/public.py:198-205` (`return response.content, ...`), `apis/photos/google.py:308-312`, `apis/locations/google/maps.py:501-513` (satellite) and `:556-560` (street-view, only a lower-bound placeholder check) all fully buffer the upstream body on a request/task worker. Sibling code shows the intended pattern: `read_capped` (`locations/google/places.py:188`, `locations/basemap_vendor_tiles_gateway.py:64`) and `read_limited` (`immich/gateway.py:183`). A large or hostile upstream body is a worker-memory event. Medium.

Partially known: the archive's P153 resolution (`PROBLEMS-ARCHIVE.md:846-847`) already notes "the Flickr download's uncapped read is a size problem"; the Google Photos/Maps instances are new. Filed to track all four to the capped pattern.

## P357 — Provider API keys travel as query params, so `raise_for_status()` writes key material into exception text

`id: P357` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (apis), verified per client.

`apis/locations/azure/gateway.py:48-51` puts `subscription-key` in `request_params` then calls bare `raise_for_status()`; `requests`' `HTTPError` string embeds the full encoded query string, so any uncaught/out-of-process-logged failure carries the key. Same shape: `locations/google/places.py:41-51` (`"key": self.api_key`), `locations/google/maps.py:430-437` (directions) and `:501-512` (static map). Header-authenticated clients (VirusTotal `x-apikey`, Places-New `X-Goog-Api-Key`, Immich `x-api-key` with redirect-host pinning) lack the exposure — and the log-ledger side is clean (only base URL logged, `rate_limiter.py:1197`). Medium (key-in-error-text reaches logs/crash reports; rotation-bounded).

No `PROBLEMS.md`/archive hit for subscription-key/query-key logging. Appears new.

## P358 — Three more P5 instances: `update_trip`, trip-activity edit, and `save_article` write whole rows

`id: P358` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (geo/pins/trips/wiki); P5's table lists only `controllers/*` handlers and P326/P345 never mention these files — verified new instances.

1. `services/trips/trip_crud.py:229`: `update_trip` assigns only the presence-keyed subset (`name/description/start_date/end_date`, `:221-228`) then bare `trip.save()`. Two organizers editing different fields round-trip each other; `updated` bumps on no-ops. Medium.
2. `services/trips/trip_activities.py:640-642`: a no-change guard (`_editable_state == before: return`) then bare `activity.save()` — concurrent edits to different columns (title vs schedule vs place) last-writer-win. Medium.
3. `services/wiki/articles.py:549-553`: sets `content/content_html/toc/last_edited_by` then bare `article.save()`; the pin-existence lock at `:540` does not lock the article row. History survives via `ArticleRevision`, but the live row is last-writer-wins. Low-medium.

P5 (open) is the umbrella; P326 (models) and P345 (onboarding/profile) are siblings. Cite all four — `see P5, P326, P345 and P358 in docs/PROBLEMS.md`.

## P359 — Trip visibility runs a friendship query per COMMON_FRIEND activity, and the trip map fans out a full query per linked child trip

`id: P359` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (trips), verified by reading both loops. One entry, compounding layers of one render cost.

- `services/trips/trip_visibility.py:78-87`: inside `for act in c_friend_acts`, one `Friendship.objects.filter(...)` query per activity (plus per-act flattening `:88-92`). Called by every trip-activities-panel and trip-map render; cost scales with trip size. Medium.
- `services/trips/trip_map.py:64-67`: per linked child trip, a full `activity_queryset(child_trip)` load plus `viewer_hidden_activity_ids` — which re-runs the per-activity friendship queries above. N extra loads plus N extra visibility evaluations per map request. Medium (P277 shape, trips flavor).

Archive `trip_visibility.py` mentions concern semantics/strictness, never query count; `GOALS_CODE_AUDIT.md` cites `trip_map.py:70,96` only for the title-visibility leak; P277 is pin panels, not trips. Appears new.

## P360 — Scan pipeline recomputes each (device, wiki) pair once per entry, and one boundary vote re-resolves every location in the polygon inline

`id: P360` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (device_scan/geo), verified by reading both loops. One entry, two unbounded-fan-out paths.

1. `services/device_scan/pipeline.py:26-42`: `for entry in entries: for wiki in wikis_containing_point(entry.location): recompute_wiki_device_markers(device, wiki)` — `wikis_containing_point` (`wiki_lookup.py:25`) is itself a spatial query per entry, and each recompute re-reads all entries, re-clusters, and writes markers. An upload with K entries at one wiki pays K spatial lookups and K full recomputes, each independently crashable with no surrounding `atomic`. Medium (background, but multiplicative).
2. `services/geo/boundary_voting.py:183` → `apply_winning_boundary` (`:150-155`) → `resolution.resolve_locations_in(polygon)` (`services/places/resolution.py:64-70`), looping every `Location.objects.filter(point__within=polygon)` with per-location `resolve_location_place` (own `UPDATE` + cache check) — inside the vote POST, no batching, no backgrounding, no cap. A vote on a large parcel re-resolves an unbounded set inline. Medium.

P338 covers `clustering.py` internals and marker indexes — explicitly a different loop; no `pipeline.py` entry exists. P148 is the county-sized membership bug and `PROBLEMS.md:1958` notes the 104-location move as incident color, not fan-out cost. Appears new.

## P361 — Trip/wiki change fan-out tasks are non-idempotent under at-least-once delivery, so redelivery double-notifies

`id: P361` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (tasks), verified from decorator to insert path.

`announce_trip_change_task` (`tasks.py:5442`) and `announce_wiki_change_task` (`:5465`) are `Queue.BULK` tasks that `notify_*` by inserting `NotificationLog` rows per audience member (fold supported, but redelivery re-executes the whole fan-out). Enqueue path `_enqueue` (`services/notifications/change_notifications.py:155-161`) is `transaction.on_commit` + `safely_enqueue_task` with no dedup key; `CELERY_TASK_ACKS_LATE = True` (`settings/base.py:384`) redelivers any task whose worker dies after the inserts but before ack, and the outbox (durable default) replays refused enqueues. Contrast `archive_safety_checkin` (documents idempotency) and `process_device_scan_upload` (PENDING→PROCESSING claim) — these two document none. Medium (duplicate member notifications).

The archive's chunk-541 beat-idempotency thread covers sweeps and calls these fan-outs neither way; P113/P125 do not name them. Appears new.

## P362 — Document sniffing fails open for unfingerprintable bytes, and uploaded PDFs are stored and served verbatim

`id: P362` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (media/security), verified on both paths. One entry, two halves of document trust.

1. `sniff_media_kind` returning `None` (unfingerprintable) passes: `services/security/content_sniffing.py:101-104` (`if sniffed is None or sniffed == declared: return None`) on upload and the same mismatch-only structure on the import-archive path (`import_data.py:653-663`, photo-gate only) — so a script/HTML renamed `.docx`/`.pdf` that `filetype` cannot fingerprint is stored. The office-doc half reaches the LibreOffice converter in sandbox; the PDF half is stored as-is.
2. `services/media/documents.py:64-69`: `.pdf` returns `None` (no conversion) — unlike photos (downscale + pixel/ICC normalization, `images.py:896-929`) and video (transcode/remux + tag strip). `extract_pdf_text` (`:106-117`) reads via `PdfReader` + OCR but never rewrites, so embedded JS/launch/attachment actions survive to the served `application/pdf` (media origin serves PDFs unsandboxed, `proxied_media.py:40` + `MEDIA_PIPELINE.md` §4).

Medium-low (sandbox + `pending_scan` quarantine + media-gate auth contain it; residual is polyglot/mislabeled content reaching converters and viewers' PDF renderers). The sniff fail-open is documented as intentional for docs (`content_sniffing.py:12-14`) but never filed as a bypass. Appears new.

## P363 — `reencode_stored_field` reads the whole stored file into RAM unbounded — the P341 pattern in a second file

`id: P363` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (media), verified: `services/media/stored_field.py:232-235` does `raw = handle.read()` then `reencode_image_file(io.BytesIO(raw), ...)` with no size check — the same shape as P341's `held_upload.py:229-232`, covering comment/trip-comment images and icon/avatar re-encodes, gated only by the site-wide upload cap (default 250 MB, raisable toward 900 MB). Sandbox worker-memory DoS, re-queueable. Medium, smaller blast radius than P341's icon path.

P341 names only the held path; grep for `reencode_stored_field` in `PROBLEMS.md`/archive hits nothing. Filed separately so fixing one file does not silently close the other — `see P341 and P363 in docs/PROBLEMS.md`.

## P364 — Provider clients missing call bounds: 9 Google calls without explicit timeouts, 3 unbounded pagination loops

`id: P364` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (apis), coordinator-confirmed per line. One entry, one theme (upstream call bounds), two shapes.

1. `services/apis/locations/google/places.py:50,100,128,166,186,198` and `maps.py:436,556,604` call `session.get/post` with no `timeout=` — every other gateway in the tree passes one explicitly. Mitigated, not moot: `_RateLimitedSession` forces a `(5, 30)` default (`rate_limiter.py:1221`), so these cannot hang forever; the gap is per-call budget control. Low.
2. `while True:` pagination with no page bound in `apis/calendar/google.py:266` (`list_events`), `apis/photos/google.py:268` (`list_session_media_items`), `apis/infra/github/contributors.py:88` — each exits only on empty/short page or item cap, so a provider returning full pages with a cursor forever never terminates. Contrast `immich/gateway.py:30-32,301` (`_MAX_LIBRARY_PAGES = 500` runaway guard — the pattern to copy) and the Flickr picker deadline test. Low (requires a misbehaving trusted provider, not an attacker).

No `PROBLEMS.md`/archive hit for these files and bounds. Appears new.

## P365 — Two fail-open third-party paths: breached-password check skipped on HIBP outage, USGS client proceeds unauthenticated after login failure

`id: P365` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (apis), coordinator-confirmed. One entry, one theme (fail-open on upstream failure), two sites.

1. `validators/password.py:82`: only `result is True` raises; `hibp.py:68-72` returns `None` on any failure. The docstring owns it ("skipped (fail-open) so signup/reset is not blocked by a third-party outage"). Documented availability tradeoff; worst case is a breached password accepted during an HIBP outage. Low. (Credit: the 20-bit prefix log leak noted at `hibp.py:69-70` is already fixed.)
2. `apis/locations/usgs.py:59-62`: `except Exception:` (with its own `# TODO: Catch specific exception`) logs and returns `None`, and `m2m_request` (`:77-79`) then proceeds with `headers = ... if session_token else None` — unauthenticated rather than failing closed on a credential-exchange failure. No secret leak (key travels in the JSON body) and `endpoint` takes only fixed internal strings, so no path injection. Low.

P1 covers VirusTotal scope, not these. Appears new.

## P366 — Two test-depth gaps: `route_import` has no provider-client test, AI suite pins prompt formatting while stubbing the wire

`id: P366` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (apis/AI), coordinator-confirmed. One entry, one theme (tests that cannot catch the bugs they surround).

1. Every other `apis/` family hits ≥2 hypothesis/property tests; `route_import` hits exactly one file (`test_memories_toggles.py`), which covers memory toggles, not route parsing/import (`route_import.py:19-59` is pure DB-write logic — the gap is import-path depth, not live-network risk). Low.
2. AI tests assert `"<USER_DATA>" in captured_prompt` (`test_document_pin_import.py:378-379`, `test_ai_tools_registry.py:172-173`, `test_ai_tools_trips.py:67,174-175`, plus trivia/article variants) while stubbing the gateway (`test_ai_assistant.py` `_StubGateway`; `test_inference_call_budget.py:35-39` mocks `requests.post`). Formatting-brittle, wire-blind: no test asserts the outgoing `InferenceRequest.max_tokens`, per-feature budgets, or cost recording. Low.

P335 covers harness DNS/TLS only. Appears new; natural home is PL6 batches.

## P367 — `wants_tile_copy` decodes bytes with Pillow outside the untrusted-parse guard

`id: P367` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (media), coordinator-confirmed: `services/media/remote_copies.py:233-238` opens `BytesIO(content)` with Pillow with no `@untrusted_parse` decorator in the file — so the decode never logs under `warn` and never raises under `deny` (the P116/P117 shape). Called today from the sandbox task `render_remote_image_copy` (`tasks.py:1764`) on this server's own re-encode output, so the live risk is guard-blindness plus reuse on untrusted bytes later. Low.

`PROBLEMS.md`/archive grep for `wants_tile_copy`: zero hits (P116/P117 were different call sites). Appears new.

## P368 — Data-import photo coordinates from `metadata.json` are stored with no range validation

`id: P368` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (media), coordinator-confirmed: `_decimal` (`services/import_export/import_data.py:1571-1577`) converts with no finite/range check (contrast `images.coerce_coordinates`, which rejects non-finite/out-of-range), and `:1621-1623` lands the values directly on `Image.latitude/longitude`. Archive-controlled values become map placement and spatial queries. Low (own-import path; broken placement, not cross-user leak).

No `PROBLEMS.md`/archive hit for import lat/lon validation. Appears new.

## P369 — Global-search fallback can run the full provider fan-out twice per query; trip comment matching materializes id lists per term

`id: P369` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (search), coordinator-confirmed. One entry, two cost shapes in one engine.

- `services/global_search/engine.py:107-125`: zero-hit structured queries retry via a second full `_run` (`:128-174` loops all active providers, default chain of 10) — worst case ~20 provider queries per user query, no timeout/cancellation in `_run`; `limit` bounds rows, not providers touched. Medium (cost/latency; distinct layer from P329's route throttles and H42/H49's panel throttle).
- `services/global_search/providers.py:902-906`: per-term `list(queryset.values_list(...))` materialization inside the term loop. Low-medium.

Neither the fallback double-run nor the per-term materialization is named in any open entry. Appears new.

## P370 — Repeat-billable AI paths have no memoization: vision re-bills same bytes, geocoding runs per row, web cache is exact-match brittle

`id: P370` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (AI/search), coordinator-confirmed. One entry, three un-cacheable spend paths.

- `services/ai/vision.py:139-201` (`describe_photo_keywords`, `classify_photo`): straight `api_call_slot` → provider call on every view; same bytes re-billed.
- `services/ai/document_import.py:431-481` `_geocode_pins`: up to `MAX_EXTRACTED_PINS = 200` paid `get_coordinates` calls per upload, no cache/dedupe — repeated addresses included.
- `services/search/pin_web_search.py:96-100`: cache hit requires exact 255-char `query_key` match — any name/punctuation tweak or `get_unique_search_name` drift re-fetches.

Low-medium (spend, not correctness). Archive photo-backfill/budget entries concern REData/Places budgets, never these three. Appears new.

## P371 — Two cryptographic-hygiene notes: trip slug suffix is MT `random` over 90k, unknown-vs-legacy API-key probes are timing-distinguishable

`id: P371` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (core/auth), coordinator-confirmed. One entry, two info-level notes that constrain future design.

1. `services/core/slugs.py:42-44,217-218`: public slug collision suffix is `random.randint(2, 90000)` — Mersenne Twister over ~16.5 bits, enumerable and predictable after observations. Not a credential today (views still gate on membership), but it must never become one. Info/low.
2. `services/auth/api_keys.py:192-200`: unknown-prefix probes cost one SHA-256 ("hash anyway, so an unknown prefix costs what a known one does") while legacy-prefix probes fall into PBKDF2 `check_password` (`:124-130`, ~0.9 s per module docstring) — timing distinguishes "prefix names a legacy row" from "unknown prefix". Narrowing as legacy rows upgrade on first use. Info.

P146 covers legacy-key CPU cost/migration, not the oracle; no slug-entropy entry exists. Appears new.

## P372 — Three N+1 leftovers: enrichment density counts, scan-ingestion device lookups, floorplan per-row saves

`id: P372` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (locations/device_scan/floorplans), coordinator-confirmed per site. One entry, one bug class (P5/N+1 family), three files.

1. `services/locations/enrichment.py:398-400` + `_nearby_density_score` (`:420-422`): one `point__dwithin` count per shortlisted candidate per source per cycle — 3× the per-run budget in extra spatial counts. Background job, low-medium.
2. `services/device_scan/ingestion.py:69-70`: one `get_or_create_for_mac` round-trip per device in the upload (module comment `:57-59` notes ~200 entries/uploads and celebrates batching only the marker lookup). Synchronous request path the module calls "deliberately lightweight". Medium.
3. `services/floorplans/serialization.py:388,406,448`: per-pool/per-item bare `source.save()`/`reference.save()`/`row.save()` plus `references.set()`/`labels.set()` per item — one autosaved edit fans out to O(pools + items) round trips on the request path (editor autosaves on debounce). Low.

P338 covers marker-path indexes/atomicity, not these; no `ingestion.py`/floorplan-save/enrichment-density entries exist. Appears new.

## P373 — Three task retry gaps: Stripe sync retries hot, CRIS extraction is soft-only with no retry, DM geocode swallows retryable failures

`id: P373` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (tasks/messaging), coordinator-confirmed. One entry, one theme (retry policy), three tasks.

1. `tasks.py:5055-5056`: `sync_stripe_subscriptions` catches `APIConnectionError/RateLimitError/APIError` into bare `self.retry(exc=exc)` — bypassing the task's own `autoretry_backoff`, no `countdown`, so a sustained Stripe outage walks every page with tight retries, re-hitting the rate limiter hot. Medium.
2. `tasks.py:1571`: `extract_cris_attachments` declares only `soft_time_limit` (720 s; hard falls through to the 3600 s global) with no `autoretry_for` — a soft-kill mid-list abandons the tail (prefix already merged, each in its own `atomic`), and transient REData blips are swallowed as `continue`. Low-medium. (P304 is preview parse limits, not CRIS retry.)
3. `services/messaging/dm_location_detection.py:274`: bare `except Exception → return None` inside `_geocode_address` converts timeouts/DNS/5xx into permanent silence — `detect_dm_address_mentions` (`tasks.py:3675`, INTERACTIVE, `autoretry OSError`) never sees the `OSError`, so autoretry never fires and no sweep replays it. Low-medium (missed location-share records).

No Stripe-retry/CRIS-retry/DM-geocode entries in either PROBLEMS file. Appears new.

## P374 — Fact recompute's bare `save()` can clear a concurrently-set `needs_recompute` (lost recompute)

`id: P374` · `status: open` · `updated: 2026-10-06`

Found by spark-audit services batch 3 (facts), coordinator-confirmed: `_recompute_locked` sets `fact.needs_recompute = False` under `select_for_update` then bare `fact.save()` (`services/facts/confidence.py:234`), while the writer path does `evidence.save()` + `Fact.objects.filter(pk=...).update(needs_recompute=True, ...)` (`services/facts/evidence.py:142-145`). Evidence committed after the recompute read but before its save gets its flag overwritten back to `False`, and the new evidence waits until something else sets the flag (bounded by the 10-min `sweep_stale_fact_confidence`, `tasks.py:4645`). Medium (stale confidence/status up to 10 min).

No fact-confidence lost-update entry in either file. Appears new.

## P375 — Friendship block/pending state-machine gaps: `block()` keeps the wrong orientation, `PENDING` can never be accepted

`id: P375` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M2), verified by reading the methods. One entry, two defects in one state machine.

1. `Friendship.block()` (`models/friendship/model.py:249-255`) reuses the existing row via `between()` then `_set_status(BLOCKED)` without re-orienting — unlike `request()` (`:119-131`), which explicitly swaps direction to match who asked. A block placed on an inbound request leaves the *blocked* party as `from_profile`, so `Profile.has_blocked` (`profile/model.py:933-944`, filters `from_profile=self`) returns False for the true blocker and `_barred_subject_pks` (`:964`) misses the inverted row. High.
2. `accept()` (`model.py:206-211`) re-reads status and refuses anything but `REQUESTED`, while `has_pending_request_to` (`profile/model.py:980-984`) treats `(REQUESTED, PENDING)` as pending (opening visibility via `allow_pending_request`), `can_request` (`friendship/meta.py:29-31`) excludes `PENDING` from re-requestable states, and decline/ignore/remove transition unconditionally. A `PENDING` row opens gates as "unanswered" yet can only be declined, never accepted. Medium.

P279 covers only *legacy* rows and claims direction "normalises now" — this path still does not. Appears new.

## P376 — Safety escalation gaps: a missed sweep skips the final warning, and opt-out identity matching disagrees with notify targeting

`id: P376` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M2), verified by reading both querysets. One entry, two safety-notify defects.

1. `overdue()` (`models/safety/queryset.py:36-50`) deliberately includes `SCHEDULED` rows "so a missed or failed `send_due_checkin_reminders` run can't prevent escalation" — but `due_for_final_warning()` (`:52-69`) requires `status=AWAITING_CHECKIN`, and `due_for_reminder()` (`:22-34`) excludes rows past `overdue_at`. One beat tick missed past the grace point jumps SCHEDULED → escalated with `final_warning_sent_at` never set: zero owner notice before emergency contacts. High.
2. `blocks_notification()` (`:253`) matches `Q(contact_profile=...) if contact_profile else Q(email__iexact=email)` while `reaching()` (`:180-189`) matches profile OR `email_normalized__in=verified_addresses`. A profile-linked row never matches an email-scoped opt-out by the same human, and the raw-`email__iexact` branch compares against the *normalized* column (`normalize_email` strips Gmail dots/`+`), so `j.ohn+tag@gmail.com` notifies despite opting out as `john@gmail.com`. `DATA_ENCRYPTION.md:266` confirms `email__iexact` "is the entire suppression mechanism". High.

No entry on either interaction; archive opt-out hits concern magic-link double-clicks. Appears new.

## P377 — `Article.editable_by()` grants every wiki article to every profile at model level

`id: P377` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M3), verified: `models/article/model.py:94-105` — pin branch checks `pin.profile_id == profile.id`; wiki branch is bare `return self.wiki is not None`. No wiki-access check (`_domains_given_pins`), no trip/concealment check — despite the module docstring (`:1-3`) promising "anyone **with access** may edit it". Any caller trusting this method as the permission gate lets strangers write community pages. High.

`editable_by` appears in `PROBLEMS.md`/archive only on an unrelated audit-report path. Appears new.

## P378 — Location mentions derive only in `save()`, so bulk writes silently desync a visibility gate

`id: P378` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M3), verified: `LocationMentioningModel.save()` (`models/comments/location_mention.py:60-72`) re-derives mentions (correctly skipping when `update_fields` lacks `text`), with no `bulk_create`/`bulk_update` override and no `QuerySet.update()` interception. The visibility gate reads these rows (`comments/queryset.py:65-70`, `trips/queryset.py:369-374`), so any bulk write of `text` (import, moderation, backfill) leaves stale `location_uuid` rows: a comment naming an unpinned place stays visible to all (leak), or a removed mention keeps hiding it (over-hide) — failing silently either way. The in-repo correct pattern exists (`LabelQuerySet.bulk_create/bulk_update` repeat coercion, `labels/queryset.py:34-73`, precisely because "bulk_create does not call save()"). High (privacy gate).

No mention/`sync_location_mentions` hit in `PROBLEMS.md`/archive. Appears new.

## P379 — Five voting-integrity gaps: trivia event-log contradiction, cross-round consensus FK, unvalidated stat votes, service-only public-vote eligibility, reaction toggle race

`id: P379` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M6), coordinator-verified per site. One entry, one theme (votes counted wrong), five models.

1. `TriviaQuestionVote` docstring promises an event log — "the same profile can be asked the same question again … fresh `NO_REACTION` backfill each time" (`models/trivia/model.py:153-156`) — but `Meta` enforces `UniqueConstraint(question, profile)` (`:182-184`). The second impression cannot be recorded; one of the two statements is wrong. High. (Nearest neighbor P326 is a different defect in the same file.)
2. `ConsensusVote.chosen_answer` (`models/consensus/model.py:408-413`) has no constraint tying `chosen_answer.round` to the vote's round; the guard is application-only (`services/consensus/voting.py:57-58`). A cross-round vote tallies against a foreign answer. Medium.
3. `WikiStatVote.cast()` (`models/wiki_stat_vote/queryset.py:123-130`) `update_or_create`s caller-supplied `field`/`value` although the docstring says callers validate — validators (`model.py:31`, 1–5) never run without `full_clean`, so garbage fields and out-of-range votes feed `composite()`'s `Avg`. Medium.
4. `PublicPinVote` eligibility ("only profiles with a root pin at the candidate's location", `models/public_pins/model.py:79-80`) lives entirely in `services.pins.public_pins`; the model has only the `(candidate, profile)` unique pair (`:109`). Stale/ineligible/post-decision ballots inserted via ORM/admin count in `tally()` (`queryset.py:68-74`). Medium — the protected action is the site's strictest gate by design.
5. Reaction toggle (`services/comments/comments.py:303-308`) reads `existing()` then deletes-or-creates; two concurrent double-taps both read `None`, both create, and the per-target unique constraints (`reactions/model.py:71-90`) turn the loser into an unhandled 500 instead of one toggle winning. Medium.

None in `PROBLEMS.md`/archive (archive `:734` notes the reaction constraints exist, not the race). Appears new.

## P380 — Three lookup-cache losses and races: `record_search_result` drops longitude, `SearchHistory`/`ScannedDevice` `get_or_create` races

`id: P380` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M5), coordinator-verified. One entry, one file family (idempotent lookup caches), three defects.

1. `EpaFacility.record_search_result` (`models/epa_facility/model.py:67-88`) takes `latitude` but no `longitude` — unlike sibling `record_detail_result` (`:91`) — so a search-sourced sighting can never populate the `longitude` column that `idxdb_epafac_lat_lng` (`:46`) and nearby-facility geo queries read. A never-DFR-enriched facility stays longitude-less permanently. Medium. (P327 covers the `data`-merge race in these methods, not the missing coordinate.)
2. `SearchHistoryManager.record` (`models/search_history/queryset.py:69`) is a bare `get_or_create` against `uniq_search_history_profile_query` (`models/search_history/model.py:39`) — two concurrent first-time searches 500 on `IntegrityError`, where `EpaFacility._get_or_create_row` (`:124-137`) shows the hardened pattern. Low-medium. (P328 is the case-dedup gap, different defect.)
3. `ScannedDeviceManager.get_or_create_for_mac` (`models/device_scan/queryset.py:44`) races on the unique MAC column (`models/device_scan/model.py:87`), called per device per upload in the ingestion loop — exactly where concurrent uploads of the same new device collide. Medium (request-path 500). (P338 covers marker indexes/transactions, not this race.)

No race entries for any of the three in `PROBLEMS.md`/archive. Appears new.

## P381 — Two caches without freshness: `RemoteImageCopy` can never refresh, `GooglePlace` rows are immortal

`id: P381` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M5), coordinator-verified. One entry, one theme (cached rows that cannot go stale gracefully), two models.

1. `RemoteImageCopy` docstring: "It has no expiry: once stored, the source is never fetched again" (`models/remote_image_copy/model.py:22`), keyed by `url_digest` unique (`:25`) — while `edition` (`:27`, "which edition of a changing picture this is, e.g. the month of a current-imagery export") is in no uniqueness or lookup key. A second edition under the same URL collides with the first, and either way the bytes freeze at first fetch — the named "current-imagery export" case serves a permanently stale month. Medium. (P330 covers the view's anonymous access, not staleness.)
2. `GooglePlace` (`models/google_place/model.py:12-42`) is coordinates + `cached_place_name`/`cid`/`place_id` with no `fresh_since`/`stale_after`/invalidation — contrast `LocationCache` (`fresh_since`/`get_fresh`/`is_stale`, per-source `max_age`). A renamed place, corrected CID, or superseded `place_id` serves forever, and the `(latitude, longitude)` unique pair mints duplicate immortal rows for near-identical coordinates instead of refreshing one. Medium. (P13 is the pin-detail TTL knob, adjacent but different claim.)

No edition/TTL/expiry entries for either in `PROBLEMS.md`/archive. Appears new.

## P382 — Four markup bookkeeping gaps: `unattached()` misses relations, visits lack the map-removal tombstone, labels unsanitized, overlays can belong to nothing

`id: P382` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M5), coordinator-verified. One entry, one feature area (markup/map attachments), four gaps.

1. `MarkupMapQuerySet.unattached()` (`models/markup/queryset.py:71-84`) filters four relations null, but `MarkupMap.attachments` (`models/markup/model.py:234-242`) enumerates six — maps attached only via the secondary safety-check-in M2M or a DM read as "unattached (drafts/leftovers)", eligible for draft-sweep deletion. Medium-low (silent map+items loss).
2. The delete signal tombstones comments/trip-comments/DMs (`models/markup/signals.py:72-83`) but not visits: `PinVisit.markup_map` is `SET_NULL` (`models/visits/model.py:46-52`) with no `map_removed` field (comments `:68` and DMs have one), so a visit whose map is deleted renders "no map" instead of "map removed". Low-medium.
3. `PinMarkup.label` is truncated (`map_snapshot.py:122-124`) but never sanitized: model `save()` (`models/markup/model.py:485-490`) never runs `full_clean()`, so `MaxLengthValidator` (`:419`) never fires and the `TextField` is unbounded; `to_json()` (`:492-511`) emits it verbatim and search indexes it. Mitigated today (renderer escapes via `escHtml`, `markup-toolbar.ts:230-337`) — defense-in-depth for API/search-snippet consumers. Low. (P332 is CSV injection, different surface.)
4. `MapImageOverlay.parent_pin/parent_wiki` are both nullable (`models/map_overlay/model.py:102-115`) with constraints covering only the image-vs-tile invariant (`:137-142`) — unlike `CustomLayer`, which at least documents its exactly-one expectation. A controller bug or direct write leaves an ownerless overlay: storage + `Image` rows leaked with nothing to display or delete them. Low-medium.

No overlay-parent/`unattached`-completeness entries in `PROBLEMS.md`/archive. Appears new.

## P383 — Four unenforced model invariants: comment hosts, safety senders, contact emails, boundary votes

`id: P383` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M2/M3/M5), coordinator-verified per site. One entry, one bug class (documented invariants the DB does not hold).

1. `Comment` documents "exactly one of pin or wiki" (`models/comments/model.py:17-20`), both FKs nullable (`:31-44`), `Meta` (`:98-101`) constraint-free — orphans (invisible everywhere) and double-hosted rows (double-rendered/counted) are storable. Siblings enforce it (`Article.article_exactly_one_host`, `CommentLocationMention.ck_cmtloc_exactly_one_owner`). Medium.
2. `SafetyCheckinMessage` documents exactly-one-sender (`models/safety/model.py:618-627`) with no `CheckConstraint` (`Meta :655-657` is ordering only) — both-null and both-set rows insertable, where `EmergencyContactDefault` (`:139-144`), `SafetyCheckinContact` (`:471-476`), and `SafetyContactOptOut` (`:519-545`) all enforce their XORs. Medium.
3. Those XORs accept `email=""`: `Q(contact_profile__isnull=False) ^ Q(email__isnull=False)` (`safety/model.py:140-144`, same shape `:472-476`, `:520-523`) passes an empty address with no profile — a contact reaching nobody and matching nothing, given `EmailField(null=True, blank=True)`. Low.
4. `BoundaryVote.boundary` "must be one of the place's own candidates — enforced at the endpoint, since a CHECK constraint can't join" (`models/boundary_vote/model.py:24-27`); the unique pair (`:60`) scopes the voter, not the choice. Any non-endpoint writer records place A's voter endorsing place B's geometry into the recency-weighted tally. Low-medium, latent (endpoint is the only writer today). (P360 is the re-resolution *cost*, not tally correctness.)

None in `PROBLEMS.md`/archive. Appears new.

## P384 — Sharing/suggestion integrity gaps: `PinShare` dedup holes, merge-suggestion races, suggestion CASCADE, cross-profile list items

`id: P384` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M3), coordinator-verified. One entry, one feature family (sharing/suggestions/queues), four gaps.

1. `PinShare`'s only backstops are pending + map-detected `(pin, to_profile)` uniques (`models/pin_share/model.py:239-253`): concurrent DM detections (`origin="dm_detected"`) both insert; location-only shares (`pin=None`, `:63`) are NULL-keyed so constraints never fire — repeat detections of one address duplicate unboundedly; accepted/rejected rows re-share without limit. `reusable_for()` (`queryset.py:91-104`) is read-then-create in services, not a constraint. Medium.
2. `PinMergeSuggestion`: order-independent pending check-then-create (`queryset.py:106` + `create`) with no constraint behind it, so a repeated trigger (the docstring's own re-import example) races into duplicates; nothing ties pins to `profile` or requires `suggested_survivor ∈ {pin_a, pin_b}`; `status` (`:48`) transitions freely while `is_actionable` (`:68-75`) is advisory. The sole constraint (`~Q(pin_a=F("pin_b"))`, `:85-92`) is not a dedup. Medium-high.
3. `PinSuggestion.pin` is `CASCADE` (`models/pin_suggestions/model.py:106`) while both sibling queues use `SET_NULL` with surviving-row comments — deleting the matched pin erases the review-queue row and its audit trail; no `pin.profile == profile` invariant and no accept-transition guard (double accept re-runs creation in services). Medium.
4. `PinListItem` uniqueness (`models/pin_list/model.py:116-120`) stops double-adds but nothing ties `pin.profile` to `pin_list.profile` (`:21-26` documents "their own Pins") — cross-profile rows reference (and thereby leak the existence of) another user's pin id, and smart-list sync (`pin_list/signals.py`) assumes single ownership. Medium. Plus `LocationExposureManager.record()` check-then-`get_or_create` with no `IntegrityError` retry (`models/pin_share/queryset.py:51-69` vs the in-repo label pattern `labels/queryset.py:99-162`) — concurrent share-accept propagations 500 on the accept path. Low-medium.

No `dm_detected`/merge-suggestion/`PinSuggestion`/cross-profile-list entries in `PROBLEMS.md`. Appears new.

## P385 — Five more P5 instances: `Image`, `Place`, `TripInvitation`, `ProfileEmail`, and `NotificationLog` saves ignore `update_fields`

`id: P385` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M1/M2/M3), coordinator-verified per method. One entry, five instances of the P5 class; none in P5's table, P326, P345, or P358.

1. `Image.save()` (`models/images/model.py:357-366`) computes `original_filename`/`filename_taken_at` in memory then bare `super().save()` — a scoped `save(update_fields=["caption"])` with a newly attached file silently drops the captured filename metadata (lost write, not just stale). Medium-low.
2. `Place.save()` (`models/place/model.py:209-219`) full-row-writes then unconditionally second-`UPDATE`s the self-anchor on every root insert even when the caller set `domain_root` — and scoped saves stomp `geometry`/`building_child_count` maintained by background writers. Low-medium.
3. `TripInvitation.save()` (`models/trips/invitation.py:93-96`) bare-saves after defaulting `expires_at` — answering one question via `save(update_fields=["trip_response"])` whole-row-writes, so the two independent answers (`trip_response` vs `friend_response`) last-writer-win. Medium.
4. `ProfileEmail.save()` (`models/profile/email.py:54-56`) recomputes `normalized_email` then bare-saves — scoped saves write without the match/dedup companion (stale) or pay full-row writes excluding `email`. Medium. (P326's `FriendInvitation` sibling; the in-repo correct pattern is `safety/model.py:448-455`.)
5. `NotificationLog.save()` (`models/notifications/model.py:90-105`) clips `title`/drops unsafe `url` in memory then bare-saves — a scoped `save(update_fields=["status"])` leaves DB and instance diverged, and an "unsafe URL dropped" still stored. Low.

P5 (open) is the umbrella; P326/P345/P358 are siblings. Cite all five — `see P5, P326, P345, P358 and P385 in docs/PROBLEMS.md`.

## P386 — Three missing composite indexes: media-relevance aggregates, memory "needs attention", pin-note ordering

`id: P386` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M1), coordinator-verified. One entry, one theme (query-shaped indexes Django's FK defaults do not cover), three sites.

1. `MediaRelevance.vote_scores()` filters `(location, source, is_vote)` and aggregates every voter's rows (`models/images/queryset.py:477`), but the only index starts at `profile` (`idxdb_medrel_profile_loc`, `models/images/relevance.py:58-66`; the 4-column unique serves `for_gallery`'s profile-led path). Runs per provider gallery per wiki render. Low-medium. (`Index(fields=["location", "source"])`, optionally vote-conditioned, matches the `idxdb_place_exttag_placesrc` pattern.)
2. `ImageQuerySet.needs_attention()` (`models/images/queryset.py:410-427`) filters `profile` + five NULL/boolean conditions (`visit__isnull`, `organize_dismissed`, `pin__isnull`, `wiki__isnull`, `pin_suggestion__isnull`) ordered by `-created` — rendered per login (Memories queue) — with no covering index (`Image.Meta :527-535` indexes other profile-led shapes). A partial composite on the all-unfiled shape fits the file's partial-index idiom. Low-medium.
3. `PinNote` orders by `(-created, -pk)` (`models/pin/note.py:36-40`, tie-break load-bearing for display) with `indexes = []` — only the default FK B-tree exists, so every note list sorts at read time. `Index(fields=["pin", "-created", "-pk"])` is the standard fix. Low.

No `PROBLEMS.md`/archive entries on these access paths. Appears new.

## P387 — Three `Location` model smells: shared mutable `Point(0, 0)` default, double-query nondeterministic nearby pick, DB-writing property setters

`id: P387` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M1), coordinator-verified. One entry, one file, three smells.

1. `point = PointField(geography=True, default=Point(0, 0))` (`models/location/model.py:60`) evaluates once at import — every default-constructed `Location` shares one mutable GEOSGeometry. `save()` (`:491-494`) overwrites from lat/lon in practice, which is why it hasn't bitten; any in-place `loc.point.x = …` corrupts the process-wide default. `default=lambda: Point(0, 0)` removes it. Low.
2. `get_nearby_or_create` (`models/location/queryset.py:196-203`) runs `exists()` then `first()` — two spatial queries for one question — unordered (two callers can snap to different rows, undermining the dedup) and a row deleted between the calls yields an unexpected `(None, False)`. Single ordered `.first()` fixes all three. Low. (Archive `:2409-2413` covered only the IntegrityError guard, now fixed at `:217-224`.)
3. `cached_place_name`/`cid` property setters (`models/location/model.py:168-202`) run `GooglePlaceService` get-or-create plus a raw `queryset.update()` bypassing `save()`, slug sync, and versioning — invisible at the assignment site, and the `value=None` path passes `fetch_if_missing=True` (a hidden synchronous request, the class the getter's own docstring `:205-210` warns against). Low-medium.

No `PROBLEMS.md`/archive entries on any of the three. Appears new.

## P388 — Subscription/billing model gaps: unvalidated duration crashes redemption, join-email uniqueness is check-then-act, trial grants on a permissive default

`id: P388` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M4), coordinator-verified. One entry, one subsystem (subscriptions/billing/email), three gaps.

1. `duration_months` is a free `CharField` (`models/subscriptions/model.py:261`) and `duration_as_int` (`:273-276`) is bare `int(...)` — the writer passes the POST value through raw (`services/social/friendship.py:696` ← `controllers/friendship.py:550`, no numeric check) and redemption calls it unguarded (`services/social/friend_invitations.py:228`). Any non-numeric string stored → 500 at accept-time, not invite-time. Medium.
2. "One join email per address ever" (`models/email_log/model.py:30-32`) has indexes but no `UniqueConstraint` (`:63-68`); enforcement is `exists()` pre-check (`services/security/email_safety.py:142-155`) then send-then-`create` (`:205-215`). Concurrent invites both pass and both send. Low-medium. (P355 is the reservation leak in `userprofile.py`, different defect.)
3. `threshold_met` defaults `True` (`models/billing/model.py:52`) and is recomputed only at each successful Stripe charge (`subscriptions/model.py:281`), while `is_billable` includes `TRIALING` and `grants_access = (is_billable and threshold_met) or banked` (`:89-106`). A $0-pledge trial grants below the role's minimum until the first charge. Medium-low.

Archive mentions cover `granted_by` CASCADE, absolute-expiry non-stacking, and webhook idempotency — none of these three. Appears new.

## P389 — Four reputation/achievement/cost integrity notes: unconstrained weights, deferred-field backfill lie, future streak day, validator-only cost bounds

`id: P389` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M4), coordinator-verified. One entry, one theme (computed standing/cost the DB does not defend), four notes. All low; filed jointly so the pattern is on record.

1. `ReputationEvent.weight` (`models/reputation/model.py:86,102-107`) is an unconstrained `DecimalField` feeding `total_value()`'s `F("value") * F("weight")` sum (`reputation/queryset.py:68-69`) — writers today pass only `0.9`/`1`, nothing enforces the range, so a shell/admin `-5` silently moves standing. (D9 describes the mechanism, not a bound.)
2. `Achievement.qualifying_change()` compares `getattr(self, f"_loaded_{field}", None) != getattr(self, field)` (`models/achievements/model.py:127-143`) but `from_db` (`:163-171`) only records loaded fields — a deferred qualifying field reads as changed, enqueueing a spurious full-catalog backfill (`signals.py:249-264`; grants are idempotent, so wasted work only).
3. `current_length_as_of` (`models/achievements/model.py:349-364`) returns the stored length when `(today - last_day).days <= 1` — a future `last_day` (bare `DateField`, `model.py:291`, no past-or-today guard; clock skew/backfill) yields negative days and a phantom live streak while `is_active_today` correctly reads False.
4. Cost models carry validators but zero DB constraints (`models/costs/model.py:40-45,70-72` vs `SiteSettings.Meta`'s ~30 checks, `site_settings/model.py:724-758`): `deprecation_years=0` via ORM persists past the `MinValueValidator(0.1)` (validators need `full_clean()`) and `monthly_amortized_cost` then divides by zero. Same shape on `OperatingCost`.

No `PROBLEMS.md`/archive entries on any of the four. Appears new.

## P390 — Two E2EE model notes: group-key uniqueness voids on holder deletion, bundle-version pinning exists only in a comment

`id: P390` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M4), coordinator-verified. One entry, two low notes on key bookkeeping.

1. `GroupKeyEnvelope` uniqueness is `(key, profile)` (`models/e2ee/group_key.py:89-93`) but `profile` nulls on holder deletion (`SET_NULL`, `:68-75`). Postgres treats NULLs as distinct (cf. `Label`'s explicit `nulls_distinct=False`), so two deleted members leave two indistinguishable `(key, NULL)` rows — "one envelope per holder" and holder auditability degrade per deletion. Fail-safe direction (`with_outside_holders`, `e2ee/queryset.py:123-136`, errs toward flagged); residue is duplicates + lost attribution. Low.
2. `key_bundle.py:56-58` claims "Conversation keys record which bundle version they were sealed to" — no such field exists (`ConversationKey :21-44` carries the rotation counter `version`, not a bundle pin; neither do `GroupKey`/`GroupKeyEnvelope`). Only `E2EEPasskeyWrap.bundle_version` + `usable_for_bundle` (`queryset.py:62-72`) embodies the backstop the comment implies generally. Low (reset flow may handle rotation in views; the model layer does not).

Archive E2EE entries cover version-vs-membership, history destruction, and rewrap defects — not these. Appears new.

## P391 — Two DM/group parity gaps: no disappearing messages in groups, group shares carry pins only

`id: P391` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M2), verified against the models. One entry, two concrete parity gaps (not vibes).

1. DMs have `read_at` (`direct_messages/model.py:27`), `deleted_by_recipient_at` (`:60-61`), `sender_delete_after` (`:66-70`), `is_expired_for_recipient` (`:117-137`), and a `due_for_hard_delete()` sweep (`direct_messages/queryset.py:97-109`). Groups have only for-everyone `deleted_at` (`group_chats/model.py:197-198`) plus a `last_read_at` watermark (`:118-120`); `tombstone_text_for` (`:239-253`) has no expiry branch. A sender's disappearing-message choice does not propagate to groups, and a member cannot remove a message from their own view. Medium.
2. `GroupMessageShare` is message + recipient + `pin_share` only (`group_chats/model.py:288-310`); `DirectMessageShare` carries PIN/TRIP/FRIEND kinds with trip membership and profile recommendation (`direct_messages/share.py:21-61`, `meta.py:34-39`) including `revoke()`/expiry semantics. Trip invites and friend recommendations cannot be shared into groups. Low.

P19's parity list is images/`markup_map`/`location_mentions`/`reply_to` (`PROBLEMS.md:413-416`) — neither of these. Appears new.

## P392 — `Wiki.versioned_fields` omits relations and presentation, and deleting a wiki vaporizes its revision trail

`id: P392` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M3), verified: `versioned_fields` (`models/wiki/model.py:135-150`) lists scalar content/security/date/type fields only — `cover_photo` (`:158-164`), `parent_wiki` (`:103-109`), the `labels` M2M (`:75-79`), `place`/`location` (`:83-98`), and `color`/`icon`/`detail_*` (`:64-72`) bypass `WikiFieldRevision` entirely, so nesting moves, banner swaps, and retags leave no field revision while superseded values become unrecoverable. Paired with `WikiFieldRevision.target = FK(Wiki, CASCADE)` (`wiki/revision.py:19`) and no retention rule, deleting the wiki vaporizes the whole trail. Medium.

Not P170 (article full-copy revisions never deleted); no `versioned_fields` hit in `PROBLEMS.md`. Appears new.

## P393 — Five misc model/task gaps: calendar sync silently drops, upload retry never gives up, Immich credential rot invisible, tombstone URL bypass, rating-0 deletion

`id: P393` · `status: open` · `updated: 2026-10-06`

Found by spark-audit models batch 5 (M6), coordinator-verified per site. One entry, five lows from the long tail.

1. Calendar auto-sync abandons a trip change after 5 failed pushes (`tasks.py:446,463-464`: clears `push_requested_at`, resets `push_attempts` to 0, log warning only) — never delivered, never retried, user never told; the counter reset erases the evidence (`calendar_sync/model.py:132-133`). Contrast `UploadRetry`, which at least notifies admins. Medium.
2. `UploadRetry` has no automatic give-up for persistent non-gone failures: no cap/max-age on the model (`upload_retry/model.py:19-24`), sweep backs off to a 1-day cap (`services/media/upload_retry.py:32,258`) with explicit stay-pending policy (`:325-326`); the only non-gone exit is manual `give_up()` from the admin action (`:231`, `admin.py:348-356`). Low (arguably the P119 "wait rather than drop" design; filed because the model cannot express a bound if that changes).
3. `ImmichAccount.last_verified` (`models/immich/model.py:31`) is written once at connect (`controllers/immich.py:146`) and never refreshed — no sweep, gateway call, or import updates it, while Flickr has a token-verify method and Google accounts handle refresh. A revoked key reads healthy until use fails. Low.
4. Auto-removal tombstones match links by exact URL modulo trim (`models/auto_removals/queryset.py:14-17`, `model.py:28` "case-sensitive by nature") — trailing slash, scheme/host case, UTM/ref, or fragment variants miss the tombstone and resurrect user-deleted auto-links through the shared `was_removed` check (`:44-52`). Medium.
5. Rating 0 is legal (`models/reviews/model.py:16`, 0–5) but the pin-edit path treats it as clear: `if rating and 1 <= rating <= 5` (`controllers/pin_edit.py:313-314`) falls a submitted `0` through to `elif clear_rating:` (`:319`), *deleting* the review — while viewset/service paths accept 0–5. The paths disagree on what 0 means. Low-medium.

None in `PROBLEMS.md`/archive. Appears new.
