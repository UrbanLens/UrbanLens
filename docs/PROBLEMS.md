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
