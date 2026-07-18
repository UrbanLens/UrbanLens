# PROBLEMS

Bugs or quirks identified during other work but out of scope to investigate/fix at the time.
Each entry should have enough detail (repro steps, file:line, symptoms) for a future session
to pick up without re-discovering the problem from scratch.

---

## Wiki-reference custom field picker doesn't recognize boundary-mate Locations

`src/urbanlens/dashboard/services/custom_field_references.py`, `referenceable_queryset()`'s
`"wiki"` branch (currently `Wiki.objects.filter(location__pins__profile=profile)`) duplicates
the exact-Location-row-only wiki-visibility check that
`services/wiki_access.location_visible_to` had until it was fixed in commit `15e6e2e2`
(2026-07-18) to also recognize a pin at a *different* Location whose point falls inside the
wiki's own generated boundary polygon (nearly-identical coordinates routinely resolve to
distinct Location rows - see that commit's message for the full rationale).

This second copy in `custom_field_references.py` was never updated, so a user whose pin sits on
the same building as an existing wiki - but at a boundary-mate Location row, not the wiki's own
exact Location - can now see and open that wiki (round 13 fixed that), but still cannot select it
as a target for a REFERENCE-type custom field pointing at "Wiki". Under-permissive, not a
security leak (the reverse - a bug that *grants* extra access - would be far more urgent).

**Why not fixed immediately**: `location_visible_to` is a per-object point-in-polygon check
(Python-side GEOS `.contains()`), not queryset-composable. A naive fix (loop over every other
Wiki in the DB calling `location_visible_to` per row) would be a real N+1/scalability risk on a
site with many wikis. The right fix is a shared, queryset-level "locations visible to this
profile via boundary-matching" helper that both `wiki_access.py` and
`custom_field_references.py` can call - worth building deliberately rather than bolting on.

---

## Most per-event WhatsApp/SMS notification toggles are stored but never delivered (found 2026-07-18)

`NotificationPreference` has `*_whatsapp` opt-in booleans for ~10 event types (trip_updated,
friend_request, comment_reply, comment_liked, friend_accepted, added_to_trip, wiki_updated,
pin_shared, visit_suggested, ...), settable in Settings → Account — but the only delivery code
that ever reads any of them is the safety check-in path (`services/safety.py:518-521`) and, as of
the 2026-07-18 DM audit fixes, the new-message path (`services/direct_messages.py`
`_schedule_message_text_alerts` → `tasks.send_direct_message_text_alerts_if_unread`). Every other
toggle silently does nothing: a user who enables "trip updated → WhatsApp" never receives
anything. Fix pattern to follow: the DM implementation (delayed Celery task, re-check relevance,
per-streak debounce, dispatch via `services/notification_delivery.py`). Alternatively, if these
channels aren't wanted for a given event type, remove its toggle from the settings UI rather than
leaving a dead control. `docs/FEATURES.md`'s notification-matrix description was corrected to
reflect current reality.

---

## ~~Profile hero renders the bio/ghost-viewer content twice under certain conditions~~ (RESOLVED 2026-07-18 - both were test false-positives, not rendering bugs)

Originally logged as two suspected profile-page rendering bugs surfaced by failing tests.
Root-caused and closed: **neither was a real bug** - both tests false-positived on markup/script
text legitimately introduced by the bio click-to-edit slice, the same inert-string false-positive
class every other profile-page test had already been hardened against:

1. `test_bio_appears_exactly_once` - the click-to-edit bio element carries the raw bio twice in
   markup BY DESIGN (`data-raw-bio="..."` attribute + visible element text), so a raw substring
   count of 1 became impossible the moment the editor shipped. No visible duplicate ever existed.
   Test rewritten to count visible-text occurrences (`>` + bio) only.
2. `test_previewed_page_renders_as_other_user_with_banner` - the bio editor's wiring script
   contains the JS comment "// autosave endpoint the full Edit Profile page's fields already use",
   so `assertNotContains(response, "Edit Profile")` matched inert script text on every render.
   The ghost preview correctly hides the real Edit Profile button (it's gated on
   `profile.user == request.user`, and the middleware swaps `request.user` for the ghost).
   Test rewritten against script-stripped content.

Both tests pass again; nothing in production code needed changing.

---

## Identity-masking for hidden profiles: remaining render sites not yet covered (found 2026-07-18)

While building `services/identity_visibility.py` (masks a person's name/username/avatar when
their `profile_visibility` doesn't permit the viewer to see them - wired into the trip member
panel, trip activity attribution, trip comments, and both group-chat member/message surfaces),
a full-codebase audit turned up several more genuine leaks of the same shape, deliberately left
unfixed this round to keep that change reviewable. All of them show a person's raw
`username`/avatar/profile link even when that person's `profile_visibility` should hide it from
the current viewer:

1. **Trip list cards** - `templates/dashboard/partials/trips/trip_list_partial.html:64-68,86`
   (`membership.profile.user.username` in a tooltip + avatar image, `t.creator.user.username` in
   the creator badge). Whatever view renders the trip list (`TripListView` or similar) would need
   to resolve identities across every listed trip's members, not just one trip's - more diffuse
   than the single-trip render sites already fixed.
2. **Notification text bakes in the raw username at creation time**, so a template-side fix can't
   reach it: `controllers/trip.py:471` (`f'{inviter.username} invited you to join...'`),
   `services/group_chats.py:132,203` (`f"{creator.username} added you to the group..."`,
   `f"{actor.username} added you to the group..."`), `services/direct_message_shares.py:122`
   (trip-invite-via-DM). Each of these would need to resolve the inviter/actor's identity toward
   the *specific recipient* before formatting the stored `NotificationLog.message` string - the
   `suggest_mutual_connection` fix earlier in this same session (`services/connections.py`) is a
   worked example of doing this correctly (mask *before* string-formatting, not after).
3. **1:1 DM template inconsistencies** - `_thread.html` demonstrates the correct pattern for the
   thread header (`display_name`/`display_avatar_url` from `display_identity_for`) but still uses
   raw `partner.username` in four other spots in the same file: the block-confirm text (line 63),
   empty-state text (119), composer placeholder (142), and locked-composer text (166).
   `_message_items.html:32`'s quoted-reply header (`message.reply_to.sender.username`) has the
   same gap. Even the reference implementation isn't fully consistent.
4. **Pin/wiki comment author** - `templates/dashboard/partials/comments/_comment_body.html:3-12`
   renders `comment.profile.avatar`/`.username` raw. The comment *content* is correctly gated by
   `Profile.can_view_comments_from` (`controllers/comments.py:80,87,265`, an all-or-nothing
   "can see this comment at all" check using `comment_visibility`, a different field from
   `profile_visibility`) - but once a comment passes that gate, its author's name/avatar aren't
   separately masked the way `profile_visibility` would call for. Photo attachments already have
   a masking treatment for a related concern (`blurred_profiles`, same controller) showing the
   pattern exists but wasn't extended to the author identity itself.
5. **Group member-add search results** - `_group_member_results.html` /
   `GroupMemberSearchView` (`controllers/group_chats.py`) shows found profiles' real
   username/avatar unconditionally once they pass `can_direct_message`, without a
   `profile_visibility` check. Arguably lower priority (the searcher typed the exact username
   they're looking for), but worth a deliberate product decision rather than leaving it as an
   unreviewed gap.
6. **Trip comments have no `can_view_comments_from` gate at all** - unlike pin/wiki comments
   (`controllers/comments.py`), `_render_trip_comments` (`controllers/trip.py`) never checks the
   author's `comment_visibility` before including a comment. This is a different, more aggressive
   control (hides the whole comment, not just the author's identity) - deliberately not added as
   part of the identity-masking work, since the original ask was specifically about hiding
   name/avatar while keeping content visible, and changing content visibility is a separate
   product decision. Flagging since it's adjacent and was noticed in the same audit.

---

## Saved-filter include/exclude label picker: no drag-reorder or formula mode

`src/urbanlens/dashboard/templates/dashboard/partials/pin_lists/_saved_filter_label_picker.html`
+ `initSavedFilterLabelPickers()` (`_saved_filter_dialog_scripts.html`) now give the saved-filter
detail page and create/edit dialog a search-driven chip picker for include/exclude labels,
matching the main map filter sidebar's core interaction (search a suggestions list, click to add
a removable chip). Two richer features present on the main map's own filter sidebar
(`pages/map/index.html`, ~lines 4451-5099) were deliberately not ported over:

- **Drag-and-drop** (dragging a chip between the include/exclude boxes, or reordering within one).
- **Advanced filtering formulas** (boolean/grouped label logic beyond a flat include/exclude set).

**Why not fixed**: the main map's version is ~650 lines of tightly-coupled inline JS/markup
specific to that page's own closure-scoped `fp-*` element IDs, with no existing extraction seam -
pulling it out into a shared, reusable module (the right way to do this, given both consumers
should behave identically) is a substantial refactor of a large, sensitive, frequently-touched
file, not a bug-fix-batch-sized change. A smaller reusable factory already exists on that page
(`_makeLabelChipPicker`, ~line 3115, used by the bulk-edit dialog) but it also has no
drag/formula support - it's the same tier of feature the saved-filter page now has, not the
richer sidebar version. Worth a deliberate future task: extract the main map's rich picker into
a shared TS module (`frontend/ts/shared/`), used by the main map, bulk-edit dialog, and
saved-filter page alike.

---

## Data export: comments/photos/trips/direct_messages have no importer

`services/export.py` exports `comments.json`, `photos/` (files + metadata), `trips.json`, and
`direct_messages.json`, but `services/import_data.py`'s `_IMPORTERS` dispatch table has no entry
for any of the four - `_IMPORT_ORDER = ["labels", "pins", "custom_fields", "pin_lists",
"visit_history", "connections", "settings"]`. A full round-trip export→import silently drops
these four categories even though they're present (and correct) in the archive.

**Why not fixed as part of the export-completeness pass**: each of these four is meaningfully
harder than the categories that already round-trip, and this gap predates the current session's
work (these exporters already existed; only the *newer* feature fields - articles, ratings,
security indicators, media labels, expanded settings - were in scope this round, and those were
folded into the existing pins/photos/profile/settings/custom_fields importers, which now round-
trip correctly - see docs/prompts/completed.md's "Data export completeness" entry):

- **Photos**: the export copies real files into the archive's `photos/` folder. Importing needs
  to re-upload each file into storage, respect the importing profile's quota
  (`services.storage.get_quota_bytes`), and re-associate via `target_type`/`target_name` (a
  human-readable string in the export, not a uuid - matching back to a pin/wiki reliably needs
  either resolving `target_name` against `pin_uuid_map` or exporting a `target_uuid` instead).
- **Comments**: same `target_type`/`target_name` resolution problem as photos, plus a question of
  whether comments should re-target pins/wikis at all versus just being informational.
- **Trips**: `members` is a list of *usernames*, not uuids - re-creating a multi-member trip on
  import would need to resolve each member to a local Profile (only where one exists - members
  are other people's accounts, not something an import can create) and decide the right
  membership status/role for them, which raises the same "requests-not-facts" question
  `_import_connections`'s docstring already discusses for friendships.
- **Direct messages**: encrypted messages export raw ciphertext keyed to the exporting device's
  current key version (`key_version`) - importing them anywhere requires the same E2EE key
  material to still be valid, which is a materially different (and riskier) undertaking than
  the other three.

If a future session tackles this, `comments`/`photos` are the more tractable pair (fix the
target-reference shape first, in export.py, so the importer has something reliable to match on).
