# PROBLEMS

Bugs or quirks identified during other work but out of scope to investigate/fix at the time.
Each entry should have enough detail (repro steps, file:line, symptoms) for a future session
to pick up without re-discovering the problem from scratch.

---

## Profile hero renders the bio/ghost-viewer content twice under certain conditions (found 2026-07-17)

Two pre-existing test failures, confirmed unrelated to the batch that surfaced them (a click-to-edit
addition for `Profile.area`/`started_exploring` in `_profile_hero_body.html`/`profile/index.html`) by
stashing that batch's files and reproducing both failures identically against the clean baseline commit
(`65ac46f8`).

1. `test_profile_hero_avatar_sync.py::ProfileBioShownOnceTests::test_bio_appears_exactly_once` -
   `self.assertContains(response, self.bio, count=1)` fails with "Found 2 instances" of the bio text
   in the rendered `profile.view` response for the profile's own owner. The test's own docstring says
   the hero used to show a 2-line-clamped duplicate of the bio above the "About" section's full copy,
   and was supposed to have been fixed so the hero no longer renders it - this failure suggests that
   fix regressed, or a different code path re-introduced a second render site.

2. `test_profile_preview.py::ProfilePreviewFlowTests::test_previewed_page_renders_as_other_user_with_banner` -
   `self.assertNotContains(response, "Edit Profile")` fails ("'Edit Profile' unexpectedly found") when
   viewing your own profile through the "View as" ghost-simulation feature (`ProfilePreviewMiddleware`).
   The owner-only "Edit Profile" button/link should be hidden while impersonating another audience's
   view, but is appearing anyway.

Both look like they could share a root cause: something that's supposed to gate on "is this actually
the request's real owner" is instead evaluating a stale/incorrectly-scoped comparison (e.g. `profile.user
== request.user` evaluating true even under ghost-viewer simulation, or a shared hero-body include being
rendered from two different call sites on the same response). Worth checking `services/profile_preview.py`'s
ghost-viewer swap mechanism and whatever currently renders the hero body twice for #1 - possibly the two
failures are the same bug wearing two hats. Not investigated further; flagged here per CLAUDE.md's "note
problems you can't fix in the current scope" instruction rather than silently left for someone to
rediscover from a confusing sibling-test-sweep failure.
