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

## P9 — Land-use-area boundary geometry is not drawn, pending a map-overlay decision

`id: P9` · `status: open` · `updated: 2026-10-05`

Previously titled "REData's `?limit=` param is inert client-side, and land-use-area boundary geometry
needs a map-overlay decision". The `limit` half is closed: REData 0.3.0 applies `limit` per provider
(its `parse_result_limit`, clamped to 200), and `RedataLocationContextGateway.near_point` caps each
provider's rows the same way (`cap_per_provider`), so a panel's "N+" floor is decided per provider
(`redata_panel.at_limit`). Production REData still runs 5aabe887, which ignores `limit`, until 0.3.0
is deployed there; the client-side cap bounds the cache rows meanwhile.

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

## P110 — The app reads Overture from its public S3 copy, although REData serves the same themes from our own instance

`id: P110` · `status: open, decided` · `updated: 2026-10-03`

**Implemented 2026-10-03 on branch `p110-overture-via-redata`, not merged.** Inside the US it asks REData only, and abroad it keeps the public read behind the guards below. It waits on REData: both Overture near-point lookups timed out when probed (P240).

**Jess, 2026-10-02: "We're self hosting Overture. Why are we contacting external services instead of using our self
hosted instance?"** Because `OvertureMapsGateway` predates REData's Overture stack and was never moved onto it. It
reads Overture's public GeoParquet on S3, with `stac.overturemaps.org` as its index. Three callers use it:
`BoundaryProviderChain` (third, after REData and Overpass), and the `overture_building_attributes` plugin's
`get_building_attributes` and `get_nearby_places`. REData syncs Overture's buildings, places, addresses and
transportation for every US state and territory monthly (`docker/overture/README.md`). It serves them from
`/buildings/` (`OvertureBuilding`: `height`, `num_floors`, `building_class`, `subtype`, `sources`) and from the
`overture` provider of points of interest.

**Fix:** answer all three from REData. The public read stays only for coordinates outside REData's synced area, the US,
behind the guards it has now. Inside the US the app then makes no call to Overture at all, which is what this problem
was trying to bound, and the live load check becomes moot there. REData reports a missing Overture database as "no
results, not an error". So the move needs a test that an empty REData answer inside the US does not fall through to the
public read.

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

## P145 — The HRSH courtyard pin on k3s-staging got a circle, a service road for a title, a building's name as an alias, no Wikipedia article and one building in its CRIS card

`id: P145` · `status: open` · `updated: 2026-09-23` · `decision: D20` · `tests: tests/integration/specs/location/hrsh-naming.spec.ts`

Jess pinned 41.73266, -73.92736, a courtyard on the Hudson River State Hospital campus, on k3s-staging
(Location 67). Traced read-only on staging, then reproduced and fixed on `development_main`. **Open until
the fixes reach staging and its caches from before the fix refresh.**

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

**Staging after deploy:** register, CRIS and parcel-building caches from before the fix carry no
`contains_point` and name nothing until they refresh. The miss stamped on Location 67 needs a forced boundary
run (`generate_boundaries_for_location(67, force=True)`), which nobody has done from here.

## P148 — A county-sized "parcel" put strangers across the Capital District into one wiki and pin-in-common domain

`id: P148` · `status: open` · `updated: 2026-09-24` · `tests: src/urbanlens/dashboard/tests/hypothesis/test_oversized_places.py, src/urbanlens/dashboard/tests/hypothesis/test_redata_boundary_provider.py`

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

**Fixed in `356c2ca6a` and `178cc8939`.**
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
- **Production is unchecked.** The hull fallback shipped in v0.6.0 and the ranking call in v0.7.0; before
  v0.7.0, every NY parcel took the hull directly. A read-only check lists
  `dashboard_places` rows with `kind in ('parcel','site') and area_sqm > 1e7` (or `kind='building' and
  area_sqm > 1e6`). For each, count the distinct `dashboard_locations` and `dashboard_user_pins.profile_id`
  under its `domain_root_id`. Hulls below the ceiling look like 877: `ST_NPoints` under about 20, convex
  (`ST_Area(ST_ConvexHull(g)) / ST_Area(g)` ≈ 1), a `redata` Boundary row, and members more than 1 km apart.
  Once this commit is deployed, `detach_oversized_places --dry-run` gives the same list.
- **A hull below the ceiling can still exist.** 877 (13.8 km²) is only caught because it is over 10 km²;
  hulls of 8.4 km² (23, superseded) and 4.9 km² (1632) remain. The origin fix stops new ones.
- **Children the bogus parcels spawned are detached, not deleted.** 864's 500 OSM building places (an
  Overpass fetch of 5,788 buildings inside its outline) and the 359 building child pins made for
  `e2e-primary` under it are still there. 72's 743 REData building places came from the statewide
  survey-roster expansion REData fixed in `fb878e2c`. Their 478 wikis are no longer nested under the HRSH wiki.
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

`id: P182` · `status: open, upstream` · `updated: 2026-10-01` · `found by: P181's investigation, 2026-10-01`

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

**Upstream fix, not deployed.** REData joins a relation's split member ways into rings and replied (its T10) that Kirkbride
should come back as a polygon, probably merged with its Overture footprint. REData's deploy waits for v0.8.0. Nothing here
should need to change: `upsert_place` updates a building place by its provider key once the cached `parcel_buildings`
answer refreshes. If the merge gives Kirkbride a new key, place 483 is orphaned, with no Location on it: migration 0034
moved them all by containment. To close: after REData deploys, refresh HRSH's buildings and re-run the location project.

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

## P216 — Historic Newspapers shows nothing, because no page reaches UrbanLens with its text

`id: P216` · `status: open` · `updated: 2026-10-04` · `found by: P196, checking each provider's fields, 2026-10-03`

REData's half is asked for in `docs/handoffs/redata-chronicling-america-description-dropped.md`.

Since P196, a Media gallery item must name the place to be shown. A Chronicling America item has no text that could.
LoC returns a page's OCR excerpt as a *list* of strings in `description`. REData's `ChroniclingAmericaGateway` passes it
through `_strip_html`, which returns `""` for anything that is not a `str`, so the description is always empty. Its sibling
`LibraryOfCongressGateway` joins the list. What remains is the title, which is the newspaper's own dateline: "Image 7 of
River Falls journal (River Falls, Pierce County, Wis.), July 30, 1908". The Historic Newspapers tab is therefore empty
for every place.

Before P196 the tab showed everything LoC returned. On 2026-10-03 the live collection's 20 results for "Hudson River State
Hospital" were 8 printings of one 1908 syndicated article naming the hospital, and 12 pages whose excerpt does not
contain the name. LoC matches each word separately.

To fix it, REData should join the list as its LoC gateway does. Then the dateline still names the paper's town and county,
which reads as a conflict for any place elsewhere ("Pierce County" against Dutchess). Judge a newspaper page on its text
alone, for example by keeping the dateline out of `title` and `caption`, or have the source tell the judge to skip them.
Check the shape against the live collection first; it was returning 503s and timeouts on 2026-10-03, and on
2026-10-04 REData's search still ended in `chronicling_america could not be reached: ReadTimeout`.

## P240 — Inside the US the Building Characteristics panel and the chain's Overture step get nothing, because REData's Overture near-point lookups time out

`id: P240` · `status: open` · `updated: 2026-10-03` · `follows: P110`

P110's fix, held on branch `p110-overture-via-redata` (24f7ca3ce), sends every US Overture question to REData: buildings to `GET /buildings/?provider=overture`, places to
`GET /points-of-interest/lookup/?provider=overture` (`services.apis.locations.boundaries.overture.OvertureProvider`).
Probed once each against the deployed REData on 2026-10-03, at the US Capitol: `/buildings/` gave no response within
60 s, and the places lookup was a 504 from REData's proxy at 90 s. `/capabilities/` answered in 0.3 s, so REData was
up.

The likely cause is on REData's side, from reading its `main` rather than a query plan: both lookups filter with
`geometry__distance_lte` on SRID 4326 columns, which Django compiles to `ST_DistanceSphere(...) <= r`. That cannot use
the spatial index, so each request scans the whole US table.

What it costs UrbanLens until REData changes it:

- Every US Building Characteristics fetch waits 30 s (`redata_context_gateway._REQUEST_TIMEOUT`), raises an outage, and
  caches nothing, so the panel stays empty and is retried.
- The chain's Overture step defers after the same 30 s, which schedules up to `MAX_DEFERRED_RETRIES` reruns of the
  location's boundary generation.
- Each of those calls probably starts one of the scans on REData's Overture database; not checked on REData's side. `redata_buildings` is limited to
  20 calls a minute, and the places calls share `redata_points_of_interest`'s 120.

Asked of REData in [`handoffs/redata-overture-near-point-lookups.md`](handoffs/redata-overture-near-point-lookups.md),
with three smaller gaps: `roof_shape`, `roof_material` and places' `operating_status` are not ingested; the providers
claim the generous US boxes, which reach border places no shard syncs (Hermosillo, Nassau); and `buildings:read` is
not backfilled onto existing keys. The development key holds it, since `/buildings/` did not answer 403.

The P110 branch should not merge before REData's lookups answer in a few seconds. Re-probe both requests above when
REData says it has changed them.

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

## P286 — A campus pin can lose its own National Register listing, because REData answers a point with the rows last found from it

`id: P286` · `status: open` · `updated: 2026-10-04` · `follows: P228`

P228's link to NPS's record appears only when REData's `nps_nrhp` answer for the pin's point holds the listing. On
2026-10-04, HRSH's campus pin (location 97736) had none: REData's cached answer from that point was Isaac Roosevelt
House alone, while a `force_refresh=true` search from the same point found HRSH's own listing (89001166), its boundary
holding the pin.

REData keys a resource to the last point that found it, and answers a point with the rows keyed to it
([handoff](handoffs/redata-cultural-resource-cache-keyed-by-last-search.md)). A search from a neighbouring point, such
as one of the campus's building pins, moves the shared listing away. The campus point keeps a fresh but partial answer
for as long as any of its other rows stays put. It also works in reverse: a row the last live search did not find,
like Isaac Roosevelt House at about 540 m, keeps answering. The same read serves 59 of REData's providers.

UrbanLens's side is correct given a correct answer. After one forced refresh on development, the pin and its wiki
gained `National Register #89001166` and its National Archives record. The Property Records Overview named the
listing with its number, and Isaac Roosevelt House, whose boundary does not hold the pin, was not linked.

Closes when REData answers a point with what its last search there found, and HRSH's campus pin keeps its listing
after a lookup from one of its building pins.

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

## P315 — A `503 rate_limited` from REData Places is not held off for the wait it names

`id: P315` · `status: open` · `updated: 2026-10-05`

REData's T11 (`../REData/docs/infrastructure-2026-10-02-replies.md`, item 3) asks clients to keep
uncached Places searches to about 40 a UTC day and to honour `Retry-After` on `503 rate_limited`.
The first is done: `redata_places` defaults to 40 calls a day (`services/core/rate_limiter.py`). The
second is not: `RedataPlacesGateway._error_for` turns REData's rate-limited body into a
`GatewayRateLimitedError`, which carries no wait, so a caller stops its own run but the next request
asks again at once. Making it an `UpstreamBusyError` with `upstream_retry_after(response)` would let
the breaker hold it off. Not measured how often production meets it; REData's whole budget is 160 a
day, shared by every key and its own CID resolution, and raising it is Jess's call (REData P70).

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
