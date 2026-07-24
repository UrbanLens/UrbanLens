# PROBLEMS

Bugs or quirks identified during other work but out of scope to investigate/fix at the time.
Each entry should have enough detail (repro steps, file:line, symptoms) for a future session
to pick up without re-discovering the problem from scratch.

**Status as of 2026-07-23 (cleanup)**: all fully-resolved entries have been removed from this
file - resolution details live in git history (this file's prior revisions) and
`docs/prompts/completed.md`. Recently closed, for orientation: the whole PR #111 cluster
(CodeQL triage, both SSRFs, E2EE password-policy endpoint, opaque rotation member IDs,
per-recipient WebSocket payloads, media-proxy URL signing), the WhatsApp/SMS delivery wiring
for every notification toggle, trip-comment `comment_visibility` gating, campus-aware
Wikipedia search (UL-354), the Overpass pool overhaul (UL-355 + self-hosted primary +
empty-result cross-validation), Internet Archive `texts` tiles, the child-pin terminology
sweep, the compose test "pod" for DB-backed tests, and `schedule_panel_fetch`'s broker-outage
handling.

**Closed in the post-cleanup round, same day**: **PinTombstone pruning** (daily
`prune_pin_tombstones` beat task, 400-day retention in
`services.pin_sync.TOMBSTONE_RETENTION`; `pins/deleted/` now returns **410 Gone +
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
- **`test_property_records_plugin` (2)** - one test assigns `location.address`, which is
  now a read-only property (`AttributeError: no setter`); the other renders no chips
  (`'Tier 1' not found in []`), likely downstream of the same fixture rot.

**Suggested next step**: one focused session over these 9 files - none looks like a
production bug on its face (env coupling, fixture rot, template drift), but
`test_media_own_photos_preview`'s 204 and `test_profile_hero_meta_editable`'s
placeholder-visibility change deserve a real look at intent before the tests are edited to
match current behavior. The pod is left running on chiron for it.

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
`_import_direct_messages` in `services/import_data.py`, wired into `_IMPORT_ORDER` between
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
